// Package host collects host-level metadata reported on heartbeat.
package host

import (
	"bufio"
	"os"
	"strings"
	"syscall"
)

type Info struct {
	HostID       string
	Kernel       string
	BTFAvailable bool
	OSRelease    string
}

func Collect(hostID string) Info {
	return Info{
		HostID:       hostID,
		Kernel:       kernel(),
		BTFAvailable: btfAvailable(),
		OSRelease:    osRelease(),
	}
}

func kernel() string {
	var u syscall.Utsname
	if err := syscall.Uname(&u); err != nil {
		return ""
	}
	return charsToString(u.Release[:])
}

func btfAvailable() bool {
	_, err := os.Stat("/sys/kernel/btf/vmlinux")
	return err == nil
}

func osRelease() string {
	f, err := os.Open("/etc/os-release")
	if err != nil {
		return ""
	}
	defer f.Close()
	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		line := scanner.Text()
		if strings.HasPrefix(line, "PRETTY_NAME=") {
			return strings.Trim(line[len("PRETTY_NAME="):], `"`)
		}
	}
	return ""
}

func charsToString(c []int8) string {
	b := make([]byte, 0, len(c))
	for _, v := range c {
		if v == 0 {
			break
		}
		b = append(b, byte(v))
	}
	return string(b)
}
