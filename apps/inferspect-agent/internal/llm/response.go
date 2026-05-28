package llm

import (
	"encoding/json"
	"errors"
)

// Usage carries normalized token counts. For OpenAI we read
// prompt/completion/total directly. For Anthropic we map input_tokens →
// Prompt and output_tokens → Completion and synthesize Total when the wire
// payload omits it.
type Usage struct {
	Prompt     int
	Completion int
	Total      int
}

// FinishReason is whatever the provider reported in the terminal field. The
// canonicalized name in the schema is "finish_reason".
type FinishReason string

// OutputText is the assistant's text reply, concatenated when the provider
// emits a list of content blocks.
type OutputText string

// ErrEmptyResponse is returned when the body is empty or cannot be parsed.
var ErrEmptyResponse = errors.New("llm: empty or unparseable response body")

// ParseOpenAIResponse parses a /v1/chat/completions response body and pulls
// out usage, finish_reason, and the assistant's text output.
//
// Shape (non-streaming):
//
//	{
//	  "id": "chatcmpl-...",
//	  "choices": [{"finish_reason": "stop", "message": {"content": "..."}}],
//	  "usage": {"prompt_tokens": N, "completion_tokens": N, "total_tokens": N}
//	}
func ParseOpenAIResponse(body []byte) (Usage, FinishReason, OutputText, error) {
	if len(body) == 0 {
		return Usage{}, "", "", ErrEmptyResponse
	}
	var parsed struct {
		Choices []struct {
			FinishReason string `json:"finish_reason"`
			Message      struct {
				Content json.RawMessage `json:"content"`
			} `json:"message"`
		} `json:"choices"`
		Usage struct {
			PromptTokens     int `json:"prompt_tokens"`
			CompletionTokens int `json:"completion_tokens"`
			TotalTokens      int `json:"total_tokens"`
		} `json:"usage"`
	}
	if err := json.Unmarshal(body, &parsed); err != nil {
		return Usage{}, "", "", err
	}
	usage := Usage{
		Prompt:     parsed.Usage.PromptTokens,
		Completion: parsed.Usage.CompletionTokens,
		Total:      parsed.Usage.TotalTokens,
	}
	if usage.Total == 0 && (usage.Prompt > 0 || usage.Completion > 0) {
		usage.Total = usage.Prompt + usage.Completion
	}
	finish := FinishReason("")
	text := OutputText("")
	if len(parsed.Choices) > 0 {
		finish = FinishReason(parsed.Choices[0].FinishReason)
		text = OutputText(extractContent(parsed.Choices[0].Message.Content))
	}
	return usage, finish, text, nil
}

// ParseAnthropicResponse parses a /v1/messages response body.
//
// Shape (non-streaming):
//
//	{
//	  "id": "msg_...",
//	  "stop_reason": "end_turn",
//	  "content": [{"type": "text", "text": "..."}, ...],
//	  "usage": {"input_tokens": N, "output_tokens": N}
//	}
func ParseAnthropicResponse(body []byte) (Usage, FinishReason, OutputText, error) {
	if len(body) == 0 {
		return Usage{}, "", "", ErrEmptyResponse
	}
	var parsed struct {
		StopReason string `json:"stop_reason"`
		Content    []struct {
			Type string `json:"type"`
			Text string `json:"text"`
		} `json:"content"`
		Usage struct {
			InputTokens  int `json:"input_tokens"`
			OutputTokens int `json:"output_tokens"`
		} `json:"usage"`
	}
	if err := json.Unmarshal(body, &parsed); err != nil {
		return Usage{}, "", "", err
	}
	usage := Usage{
		Prompt:     parsed.Usage.InputTokens,
		Completion: parsed.Usage.OutputTokens,
		Total:      parsed.Usage.InputTokens + parsed.Usage.OutputTokens,
	}
	var text string
	for _, c := range parsed.Content {
		if c.Type == "text" {
			text += c.Text
		}
	}
	return usage, FinishReason(parsed.StopReason), OutputText(text), nil
}

// extractContent collapses an OpenAI message.content field, which can be
// either a plain string ("hello") or a list of typed parts
// ([{"type":"text","text":"..."}]).
func extractContent(raw json.RawMessage) string {
	if len(raw) == 0 {
		return ""
	}
	var s string
	if err := json.Unmarshal(raw, &s); err == nil {
		return s
	}
	var parts []struct {
		Type string `json:"type"`
		Text string `json:"text"`
	}
	if err := json.Unmarshal(raw, &parts); err == nil {
		out := ""
		for _, p := range parts {
			if p.Type == "text" {
				out += p.Text
			}
		}
		return out
	}
	return ""
}
