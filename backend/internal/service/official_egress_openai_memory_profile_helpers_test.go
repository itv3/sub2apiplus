package service

import (
	"errors"
	"io"
	"net/http"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

// 此读取器只生成字节，不持有整段正文；若丢弃桩退化为随正文扩容的读取方式，测试会立即报错。
type officialEgressBoundedProfileReader struct {
	remaining int64
	maxRead   int
	closed    bool
	readErr   error
}

func (r *officialEgressBoundedProfileReader) Read(p []byte) (int, error) {
	if len(p) > 32<<10 {
		return 0, errors.New("丢弃桩的读取缓冲超过 32KiB，疑似整段缓存正文")
	}
	r.maxRead = max(r.maxRead, len(p))
	if r.readErr != nil {
		return 0, r.readErr
	}
	if r.remaining == 0 {
		return 0, io.EOF
	}
	n := min(int64(len(p)), r.remaining)
	clear(p[:int(n)])
	r.remaining -= n
	return int(n), nil
}

func (r *officialEgressBoundedProfileReader) Close() error {
	r.closed = true
	return nil
}

func TestOfficialEgressMemoryProfileDiscardDoesNotRetainBody(t *testing.T) {
	const size = 2 << 20
	body := &officialEgressBoundedProfileReader{remaining: size}
	request := &http.Request{
		Body:          body,
		ContentLength: size,
		Header:        http.Header{"Content-Encoding": []string{"zstd"}},
	}
	upstream := &officialEgressDiscardUpstream{}
	response, err := upstream.Do(request, "", 1, 1)
	require.NoError(t, err)
	require.NoError(t, response.Body.Close())
	require.True(t, body.closed)
	require.Zero(t, body.remaining)
	require.Positive(t, body.maxRead)
	require.Equal(t, int64(size), upstream.wireBytes)
	require.Equal(t, []int64{size}, upstream.contentLengths)
	require.Equal(t, []string{"zstd"}, upstream.encodings)
}

func TestOfficialEgressMemoryProfileDiscardRejectsIncompleteBody(t *testing.T) {
	readErr := errors.New("模拟正文读取失败")
	for _, test := range []struct {
		name   string
		reader *officialEgressBoundedProfileReader
	}{
		{name: "长度不足", reader: &officialEgressBoundedProfileReader{remaining: 3}},
		{name: "读取失败", reader: &officialEgressBoundedProfileReader{readErr: readErr}},
	} {
		t.Run(test.name, func(t *testing.T) {
			upstream := &officialEgressDiscardUpstream{}
			response, err := upstream.Do(&http.Request{Body: test.reader, ContentLength: 4}, "", 1, 1)
			require.Error(t, err)
			require.Nil(t, response)
			require.True(t, test.reader.closed)
			require.Zero(t, upstream.businessRequests)
			if test.reader.readErr != nil {
				require.ErrorIs(t, err, readErr)
			}
		})
	}
}

func TestOfficialEgressMemoryProfileFixtureReusesExactBytes(t *testing.T) {
	for _, shape := range []string{
		officialEgressMemoryProfileNative,
		officialEgressMemoryProfileExplicit,
		officialEgressMemoryProfileIncompressible,
	} {
		t.Run(shape, func(t *testing.T) {
			fixture := filepath.Join(t.TempDir(), "body.json")
			first := loadOfficialEgressMemoryProfileBody(t, 32<<10, shape, fixture)
			// 已存在文件应优先使用原字节，即使第二次的目标大小不同也不能偷偷重新生成。
			second := loadOfficialEgressMemoryProfileBody(t, 64<<10, shape, fixture)
			require.Equal(t, first, second)
			require.Equal(t, shape == officialEgressMemoryProfileExplicit, gjson.GetBytes(first, "instructions").Exists())
			first[0] = ' '
			require.Equal(t, byte('{'), second[0], "不同并发槽不能共用固定正文的同一个底层切片")
		})
	}
}
