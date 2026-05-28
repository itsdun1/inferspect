// Package http reassembles HTTP/1.1 requests and responses per SSL context.
//
// We see plaintext bytes one SSL_write at a time. For small requests a single
// write contains the whole HTTP request. For larger requests it spans multiple
// writes. The reassembler stitches them until we see a complete request line +
// headers + (when Content-Length is set) body.
//
// Out of scope: HTTP/2 framing. OpenAI / Anthropic both default to HTTP/1.1
// on libssl-backed Python clients in the langchain/openai SDKs we observe in
// chat-service. HTTP/2 is a known follow-up (Phase G.5).
package http

import (
	"bufio"
	"bytes"
	"compress/gzip"
	"io"
	"net/http"
	"strconv"
	"strings"
	"sync"
)

// Request is what we hand off to the LLM identifier once a complete request
// has been seen on the SSL context.
type Request struct {
	SSLCtx  uint64
	PID     uint32
	Method  string
	Host    string
	Path    string
	Headers map[string]string
	Body    []byte
}

// Response is what we hand off after the SSL_read side has produced a full
// HTTP/1.1 response on the same SSL context.
type Response struct {
	SSLCtx     uint64
	PID        uint32
	StatusCode int
	Headers    map[string]string
	Body       []byte
}

// Reassembler is a per-(pid, ssl_ctx) buffer. Each SSL_write event is appended;
// when a full request appears we hand it to the caller and reset. The same
// instance also tracks an independent SSL_read buffer for response stitching.
type Reassembler struct {
	mu         sync.Mutex
	streams    map[key]*stream
	respStream map[key]*stream
}

type key struct {
	pid uint32
	ssl uint64
}

type stream struct {
	buf bytes.Buffer
}

func NewReassembler() *Reassembler {
	return &Reassembler{
		streams:    make(map[key]*stream),
		respStream: make(map[key]*stream),
	}
}

// Feed appends bytes from one SSL_write. Returns a list of completed requests
// (usually 0 or 1; HTTP pipelining can produce more).
func (r *Reassembler) Feed(pid uint32, sslCtx uint64, data []byte) []Request {
	r.mu.Lock()
	defer r.mu.Unlock()

	k := key{pid: pid, ssl: sslCtx}
	s := r.streams[k]
	if s == nil {
		s = &stream{}
		r.streams[k] = s
	}
	s.buf.Write(data)

	var out []Request
	for {
		req, n, ok := tryParse(s.buf.Bytes())
		if !ok {
			break
		}
		req.PID = pid
		req.SSLCtx = sslCtx
		out = append(out, req)
		// Drop the parsed bytes from the buffer.
		rest := s.buf.Bytes()[n:]
		s.buf.Reset()
		s.buf.Write(rest)
	}
	// GC dead streams. If the buffer grows past 1MB without making progress
	// the producer is likely speaking a protocol we don't understand
	// (HTTP/2, gRPC, ALPN-negotiated something else). Drop the buffer.
	if s.buf.Len() > 1<<20 {
		s.buf.Reset()
	}
	return out
}

// FeedResponse appends bytes from one SSL_read. Returns a list of completed
// responses. Handles Content-Length, Transfer-Encoding: chunked, and
// Content-Encoding: gzip.
func (r *Reassembler) FeedResponse(pid uint32, sslCtx uint64, data []byte) []Response {
	r.mu.Lock()
	defer r.mu.Unlock()

	k := key{pid: pid, ssl: sslCtx}
	s := r.respStream[k]
	if s == nil {
		s = &stream{}
		r.respStream[k] = s
	}
	s.buf.Write(data)

	var out []Response
	for {
		resp, n, ok := tryParseResponse(s.buf.Bytes())
		if !ok {
			break
		}
		resp.PID = pid
		resp.SSLCtx = sslCtx
		out = append(out, resp)
		rest := s.buf.Bytes()[n:]
		s.buf.Reset()
		s.buf.Write(rest)
	}
	// Cap response buffer at 4MB — completion bodies can be larger than
	// requests (especially for long generations).
	if s.buf.Len() > 4<<20 {
		s.buf.Reset()
	}
	return out
}

// Forget releases per-stream state. Called when we see the connection close.
func (r *Reassembler) Forget(pid uint32, sslCtx uint64) {
	r.mu.Lock()
	delete(r.streams, key{pid: pid, ssl: sslCtx})
	delete(r.respStream, key{pid: pid, ssl: sslCtx})
	r.mu.Unlock()
}

// tryParse attempts a single HTTP request out of the buffer. Returns (req,
// bytes consumed, true) on success, or (zero, 0, false) when more data is
// needed.
func tryParse(buf []byte) (Request, int, bool) {
	// Find end of headers.
	hdrEnd := bytes.Index(buf, []byte("\r\n\r\n"))
	if hdrEnd < 0 {
		return Request{}, 0, false
	}
	headerSection := buf[:hdrEnd]
	// Cheap sanity check — first line should look like METHOD PATH HTTP/1.x.
	firstLineEnd := bytes.Index(headerSection, []byte("\r\n"))
	if firstLineEnd < 0 {
		return Request{}, 0, false
	}
	firstLine := string(headerSection[:firstLineEnd])
	parts := bytes.SplitN([]byte(firstLine), []byte(" "), 3)
	if len(parts) != 3 || !bytes.HasPrefix(parts[2], []byte("HTTP/1.")) {
		return Request{}, 0, false
	}
	method := string(parts[0])
	path := string(parts[1])

	// Use net/http to parse headers — it's already in stdlib and handles edge
	// cases. We build a fake reader.
	headerBytes := append([]byte(firstLine+"\r\n"), headerSection[firstLineEnd+2:]...)
	headerBytes = append(headerBytes, "\r\n\r\n"...)
	br := bufio.NewReader(bytes.NewReader(headerBytes))
	parsed, err := http.ReadRequest(br)
	if err != nil {
		return Request{}, 0, false
	}

	// Body length. We only buffer up to a sensible cap to avoid retaining
	// huge uploads.
	contentLen := 0
	if v := parsed.Header.Get("Content-Length"); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			contentLen = n
		}
	}
	// Chunked transfer encoding: not parsed fully. Phase G.1 falls back to
	// "no body" — we still capture method/host/path which is enough for
	// provider identification but not for fingerprinting.
	bodyStart := hdrEnd + 4
	if contentLen > 0 && len(buf) < bodyStart+contentLen {
		return Request{}, 0, false
	}
	var body []byte
	if contentLen > 0 {
		body = make([]byte, contentLen)
		copy(body, buf[bodyStart:bodyStart+contentLen])
	}
	total := bodyStart + contentLen

	headers := make(map[string]string, len(parsed.Header))
	for k, v := range parsed.Header {
		if len(v) > 0 {
			headers[k] = v[0]
		}
	}

	return Request{
		Method:  method,
		Path:    path,
		Host:    parsed.Host,
		Headers: headers,
		Body:    body,
	}, total, true
}

// tryParseResponse attempts a single HTTP/1.1 response out of the buffer.
// Supports Content-Length, Transfer-Encoding: chunked, and
// Content-Encoding: gzip.
func tryParseResponse(buf []byte) (Response, int, bool) {
	hdrEnd := bytes.Index(buf, []byte("\r\n\r\n"))
	if hdrEnd < 0 {
		return Response{}, 0, false
	}
	headerSection := buf[:hdrEnd]
	firstLineEnd := bytes.Index(headerSection, []byte("\r\n"))
	var firstLine string
	var headerTail []byte
	if firstLineEnd < 0 {
		// Status line is the entire header section — no headers present.
		firstLine = string(headerSection)
	} else {
		firstLine = string(headerSection[:firstLineEnd])
		headerTail = headerSection[firstLineEnd+2:]
	}
	// Status line: HTTP/1.1 200 OK
	if !strings.HasPrefix(firstLine, "HTTP/1.") {
		return Response{}, 0, false
	}
	sp1 := strings.IndexByte(firstLine, ' ')
	if sp1 < 0 {
		return Response{}, 0, false
	}
	rest := firstLine[sp1+1:]
	sp2 := strings.IndexByte(rest, ' ')
	codeStr := rest
	if sp2 >= 0 {
		codeStr = rest[:sp2]
	}
	status, err := strconv.Atoi(codeStr)
	if err != nil {
		return Response{}, 0, false
	}

	// Parse headers via net/http by feeding it a faked response. Easier to
	// build the header map manually to avoid the cost of http.ReadResponse
	// which wants a matching request.
	headers := map[string]string{}
	headerLines := strings.Split(string(headerTail), "\r\n")
	for _, line := range headerLines {
		if line == "" {
			continue
		}
		colon := strings.IndexByte(line, ':')
		if colon < 0 {
			continue
		}
		name := strings.TrimSpace(line[:colon])
		val := strings.TrimSpace(line[colon+1:])
		// Normalize to canonical MIME-style header casing so callers can use
		// stable lookups.
		headers[http.CanonicalHeaderKey(name)] = val
	}

	bodyStart := hdrEnd + 4
	rawBody, consumed, ok := readResponseBody(buf[bodyStart:], headers)
	if !ok {
		return Response{}, 0, false
	}
	total := bodyStart + consumed

	// Optional gunzip when Content-Encoding indicates gzip.
	body := rawBody
	if enc := strings.ToLower(headers["Content-Encoding"]); strings.Contains(enc, "gzip") && len(rawBody) > 0 {
		if decoded, err := gunzip(rawBody); err == nil {
			body = decoded
		}
		// On gunzip failure, fall through with the raw bytes — downstream
		// parsers will surface the failure rather than us swallowing it.
	}

	return Response{
		StatusCode: status,
		Headers:    headers,
		Body:       body,
	}, total, true
}

// readResponseBody reads a response body out of ``after`` given the parsed
// headers. Returns (body bytes, bytes consumed from ``after``, ok).
//
// When there is neither a Content-Length nor a chunked transfer encoding the
// body is close-delimited under HTTP/1.x. We don't observe close inline (the
// SSL_read uprobe just sees byte streams), so we wait for more data
// indefinitely rather than emitting a bogus empty response on every status
// line.
func readResponseBody(after []byte, headers map[string]string) ([]byte, int, bool) {
	if te := strings.ToLower(headers["Transfer-Encoding"]); strings.Contains(te, "chunked") {
		return readChunked(after)
	}
	if v, present := headers["Content-Length"]; present {
		n, err := strconv.Atoi(v)
		if err != nil || n < 0 {
			// Malformed Content-Length — emit empty body, consume nothing
			// past the headers.
			return nil, 0, true
		}
		if len(after) < n {
			return nil, 0, false
		}
		body := make([]byte, n)
		copy(body, after[:n])
		return body, n, true
	}
	// No body delimiter we can read inline (no Content-Length, not chunked).
	// HTTP/1.1 says close-delimited body — we don't observe close so we
	// assume an empty body and consume just the headers. The parser will
	// produce an ErrEmptyResponse downstream and the request-time row stays
	// authoritative in ClickHouse.
	return nil, 0, true
}

// readChunked decodes Transfer-Encoding: chunked bodies. Returns (decoded
// bytes, bytes consumed from input, ok). When more data is needed returns
// (nil, 0, false).
func readChunked(in []byte) ([]byte, int, bool) {
	var out bytes.Buffer
	pos := 0
	for {
		// Find end of size line.
		nl := bytes.Index(in[pos:], []byte("\r\n"))
		if nl < 0 {
			return nil, 0, false
		}
		sizeLine := string(in[pos : pos+nl])
		// Strip optional chunk extension after ';'.
		if semi := strings.IndexByte(sizeLine, ';'); semi >= 0 {
			sizeLine = sizeLine[:semi]
		}
		sizeLine = strings.TrimSpace(sizeLine)
		size, err := strconv.ParseInt(sizeLine, 16, 64)
		if err != nil || size < 0 {
			return nil, 0, false
		}
		pos += nl + 2
		if size == 0 {
			// Terminating chunk. Skip optional trailer headers until the
			// blank line.
			end := bytes.Index(in[pos:], []byte("\r\n"))
			if end < 0 {
				return nil, 0, false
			}
			pos += end + 2
			return out.Bytes(), pos, true
		}
		if int64(len(in)-pos) < size+2 {
			return nil, 0, false
		}
		out.Write(in[pos : pos+int(size)])
		pos += int(size)
		// Trailing CRLF after the chunk data.
		if pos+1 >= len(in) || in[pos] != '\r' || in[pos+1] != '\n' {
			return nil, 0, false
		}
		pos += 2
	}
}

func gunzip(b []byte) ([]byte, error) {
	zr, err := gzip.NewReader(bytes.NewReader(b))
	if err != nil {
		return nil, err
	}
	defer zr.Close()
	return io.ReadAll(zr)
}
