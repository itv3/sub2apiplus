package httputil

import (
	"bytes"
	"compress/gzip"
	"compress/zlib"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"

	"github.com/klauspost/compress/zstd"
)

const (
	requestBodyReadInitCap    = 512
	requestBodyReadMaxInitCap = 1 << 20
	jsonUTF8BOMLen            = 3
	// maxDecompressedBodySize limits the decompressed request body to 64 MB
	// to prevent decompression bomb attacks.
	maxDecompressedBodySize = 64 << 20
)

// PrereadBody 回填已读取完成的请求体：作为 io.ReadCloser 可被再次顺序消费
// （multipart 流式解析），同时暴露 Bytes() 让 ReadRequestBodyWithPrealloc
// 直接返回原始切片，避免二次分配与复制。
//
// 注意：ReadRequestBodyWithPrealloc 对 PrereadBody 的快速路径不检查内部
// reader 是否已被（部分）消费——包装的字节完整且不可变，即使 reader 已被
// 流式消费过，Bytes() 也始终返回完整请求体。
type PrereadBody struct {
	body   []byte
	reader *bytes.Reader
}

// NewPrereadBody 包装一段已读取的请求体。
func NewPrereadBody(body []byte) *PrereadBody {
	return &PrereadBody{body: body, reader: bytes.NewReader(body)}
}

// Read 实现 io.Reader（转发给内部 bytes.Reader）。
func (p *PrereadBody) Read(b []byte) (int, error) {
	if p == nil {
		return 0, io.EOF
	}
	return p.reader.Read(b)
}

// Close 实现 io.Closer；请求体已在内存中，无需释放资源。
func (p *PrereadBody) Close() error { return nil }

// Bytes 返回完整的原始请求体切片。
func (p *PrereadBody) Bytes() []byte {
	if p == nil {
		return nil
	}
	return p.body
}

// ReadRequestBodyWithPrealloc reads request body with preallocated buffer based
// on content length, transparently decoding any Content-Encoding the upstream
// client used to compress the body (zstd, gzip, deflate).
// 已由 PrereadBody 回填的请求体直接返回其完整切片（零拷贝），不检查内部
// reader 是否已被消费——见 PrereadBody 的文档说明。
func ReadRequestBodyWithPrealloc(req *http.Request) ([]byte, error) {
	if req == nil || req.Body == nil {
		return nil, nil
	}
	if preread, ok := req.Body.(*PrereadBody); ok {
		return preread.Bytes(), nil
	}

	capHint := requestBodyReadInitCap
	if req.ContentLength > 0 {
		switch {
		case req.ContentLength < int64(requestBodyReadInitCap):
			capHint = requestBodyReadInitCap
		case req.ContentLength > int64(requestBodyReadMaxInitCap):
			capHint = requestBodyReadMaxInitCap
		default:
			capHint = int(req.ContentLength)
		}
	}

	raw, err := readRequestBodyChunks(req.Body, capHint, req.ContentLength)
	if err != nil {
		return nil, err
	}

	enc := strings.ToLower(strings.TrimSpace(req.Header.Get("Content-Encoding")))
	if enc == "" || enc == "identity" {
		return raw, nil
	}

	decoded, err := decompressRequestBody(enc, raw)
	if err != nil {
		return nil, fmt.Errorf("decode Content-Encoding %q: %w", enc, err)
	}

	req.Header.Del("Content-Encoding")
	req.Header.Del("Content-Length")
	req.ContentLength = int64(len(decoded))

	return decoded, nil
}

// Read bounded chunks as bytes arrive, then assemble the exact-size result.
// This avoids doubling large buffers or eagerly allocating an untrusted
// Content-Length before the corresponding bytes have arrived.
func readRequestBodyChunks(reader io.Reader, initialCapacity int, contentLength int64) ([]byte, error) {
	capacity := initialCapacity
	var chunks [][]byte
	total := 0
	for {
		chunkCapacity := capacity
		if remaining := contentLength - int64(total); remaining >= 0 && remaining < int64(chunkCapacity) {
			chunkCapacity = int(remaining) + 1
		}
		chunk := make([]byte, chunkCapacity)
		n := 0
		var err error
		for n < len(chunk) && err == nil {
			var read int
			read, err = reader.Read(chunk[n:])
			n += read
		}
		if err != nil && err != io.EOF {
			return nil, err
		}
		if n > 0 {
			chunks = append(chunks, chunk[:n])
			total += n
		}
		if err != nil {
			if len(chunks) == 0 {
				return chunk[:0], nil
			}
			if len(chunks) == 1 {
				return chunks[0], nil
			}
			body := make([]byte, total)
			offset := 0
			for _, part := range chunks {
				offset += copy(body[offset:], part)
			}
			return body, nil
		}
		if capacity < requestBodyReadMaxInitCap {
			capacity *= 2
			if capacity > requestBodyReadMaxInitCap {
				capacity = requestBodyReadMaxInitCap
			}
		}
	}
}

// ReadLenientJSONRequestBodyWithPrealloc reads a request body and normalizes
// JSON string control bytes before strict validation.
func ReadLenientJSONRequestBodyWithPrealloc(req *http.Request, maxNormalizedBytes int64) ([]byte, error) {
	body, err := ReadRequestBodyWithPrealloc(req)
	if err != nil {
		return nil, err
	}
	return NormalizeLenientJSONRequestBody(body, maxNormalizedBytes)
}

// ReadAdmittedLenientJSONRequestBody 仅供已取得整次请求内存额度的入口使用。
// 已知未压缩长度时直接读入精确大小的正文，避免分块合并的一份完整副本。
// 压缩正文直接从入站流解码，不同时保存完整的压缩原文与解压结果；未知长度的大正文
// 在非内存文件系统暂存，不能依据压缩帧内不可信的长度提前分配大内存。
func ReadAdmittedLenientJSONRequestBody(req *http.Request, maxNormalizedBytes int64) ([]byte, error) {
	return ReadAdmittedLenientJSONRequestBodyWithReservation(req, maxNormalizedBytes, nil)
}

// ReadAdmittedLenientJSONRequestBodyWithReservation 在规范化输出分配前通知准入器其确切长度。
// beforeNormalize 返回错误时不生成输出副本；原请求的预留仍由调用方负责统一释放。
func ReadAdmittedLenientJSONRequestBodyWithReservation(req *http.Request, maxNormalizedBytes int64, beforeNormalize func(int) error) ([]byte, error) {
	if maxNormalizedBytes <= 0 {
		maxNormalizedBytes = maxDecompressedBodySize
	}
	if req == nil || req.Body == nil {
		return nil, nil
	}
	if err := req.Context().Err(); err != nil {
		return nil, err
	}
	if preread, ok := req.Body.(*PrereadBody); ok {
		return normalizeLenientJSONRequestBody(preread.Bytes(), maxNormalizedBytes, beforeNormalize)
	}
	encoding := strings.ToLower(strings.TrimSpace(req.Header.Get("Content-Encoding")))
	if encoding == "" || encoding == "identity" {
		if req.ContentLength > maxNormalizedBytes {
			return nil, &http.MaxBytesError{Limit: maxNormalizedBytes}
		}
		reader := requestBodyContextReader{ctx: req.Context(), reader: http.MaxBytesReader(nil, req.Body, maxNormalizedBytes)}
		var body []byte
		var err error
		if req.ContentLength > 0 {
			body = make([]byte, req.ContentLength)
			if _, err = io.ReadFull(reader, body); err != nil {
				return nil, err
			}
			// 读取终止状态，保留底层上传错误，并拒绝声明长度之外的额外字节。
			var tail [1]byte
			if n, readErr := io.ReadFull(reader, tail[:]); n != 0 || readErr != io.EOF {
				if readErr != nil {
					return nil, readErr
				}
				return nil, errors.New("request body exceeds Content-Length")
			}
		} else {
			body, err = readAdmittedUnknownBody(req.Context(), reader)
			if err != nil {
				return nil, err
			}
		}
		return normalizeLenientJSONRequestBody(body, maxNormalizedBytes, beforeNormalize)
	}

	// 压缩传输本身与解压后的 JSON 各自受相同上限约束；读到上限后还会读取一个
	// 字节确认 EOF，超限返回 MaxBytesError，而不是把截断的 JSON 当成成功结果。
	source := http.MaxBytesReader(nil, req.Body, maxNormalizedBytes)
	contextSource := requestBodyContextReader{ctx: req.Context(), reader: source}
	var decoded io.Reader
	var closeDecoder func()
	switch encoding {
	case "zstd":
		// 解压后的字节上限不能阻止解码器先按帧头分配巨大的历史窗口。
		// 保留常用 8MiB 窗口兼容性，更大窗口最多使用本次已准入的正文上限。
		windowLimit := uint64(max(maxNormalizedBytes, 8<<20))
		decoder, err := zstd.NewReader(contextSource,
			zstd.WithDecoderConcurrency(1), zstd.WithDecoderLowmem(true),
			zstd.WithDecoderMaxWindow(windowLimit), zstd.WithDecoderMaxMemory(windowLimit))
		if err != nil {
			return nil, fmt.Errorf("decode Content-Encoding %q: %w", encoding, err)
		}
		decoded, closeDecoder = decoder, decoder.Close
	case "gzip", "x-gzip":
		decoder, err := gzip.NewReader(contextSource)
		if err != nil {
			return nil, fmt.Errorf("decode Content-Encoding %q: %w", encoding, err)
		}
		decoded, closeDecoder = decoder, func() { _ = decoder.Close() }
	case "deflate":
		decoder, err := zlib.NewReader(contextSource)
		if err != nil {
			return nil, fmt.Errorf("decode Content-Encoding %q: %w", encoding, err)
		}
		decoded, closeDecoder = decoder, func() { _ = decoder.Close() }
	default:
		return nil, fmt.Errorf("decode Content-Encoding %q: unsupported Content-Encoding", encoding)
	}
	defer closeDecoder()
	bounded := http.MaxBytesReader(nil, io.NopCloser(decoded), maxNormalizedBytes)
	body, err := readAdmittedUnknownBody(req.Context(), bounded)
	if err != nil {
		return nil, fmt.Errorf("decode Content-Encoding %q: %w", encoding, err)
	}
	// deflate 等解码器可以先于传输正文结束；继续消费受限的入站流，才能检查
	// 压缩尾部的上传错误和总字节上限，行为与原先先读取完整 wire 正文一致。
	if _, err := io.Copy(io.Discard, contextSource); err != nil {
		return nil, fmt.Errorf("decode Content-Encoding %q: %w", encoding, err)
	}
	body, err = normalizeLenientJSONRequestBody(body, maxNormalizedBytes, beforeNormalize)
	if err != nil {
		return nil, err
	}
	req.Header.Del("Content-Encoding")
	req.Header.Del("Content-Length")
	req.ContentLength = int64(len(body))
	return body, nil
}

func decompressRequestBody(encoding string, raw []byte) ([]byte, error) {
	switch encoding {
	case "zstd":
		dec, err := zstd.NewReader(bytes.NewReader(raw))
		if err != nil {
			return nil, err
		}
		defer dec.Close()
		return io.ReadAll(io.LimitReader(dec, maxDecompressedBodySize))
	case "gzip", "x-gzip":
		gr, err := gzip.NewReader(bytes.NewReader(raw))
		if err != nil {
			return nil, err
		}
		defer func() { _ = gr.Close() }()
		return io.ReadAll(io.LimitReader(gr, maxDecompressedBodySize))
	case "deflate":
		zr, err := zlib.NewReader(bytes.NewReader(raw))
		if err != nil {
			return nil, err
		}
		defer func() { _ = zr.Close() }()
		return io.ReadAll(io.LimitReader(zr, maxDecompressedBodySize))
	default:
		return nil, errors.New("unsupported Content-Encoding")
	}
}

// NormalizeLenientJSONRequestBody escapes raw control bytes that broken
// OpenAI-compatible clients sometimes place inside JSON strings.
func NormalizeLenientJSONRequestBody(body []byte, maxNormalizedBytes int64) ([]byte, error) {
	return normalizeLenientJSONRequestBody(body, maxNormalizedBytes, nil)
}

// 先扫描计长并取得额度，再一次分配输出，避免控制字符密集时反复扩容的峰值。
// 无需转义的常见正文只扫描一遍并继续共享原切片。
func normalizeLenientJSONRequestBody(body []byte, maxNormalizedBytes int64, beforeNormalize func(int) error) ([]byte, error) {
	if maxNormalizedBytes <= 0 {
		maxNormalizedBytes = maxDecompressedBodySize
	}

	body = trimUTF8BOM(body)
	if int64(len(body)) > maxNormalizedBytes {
		return nil, &http.MaxBytesError{Limit: maxNormalizedBytes}
	}

	normalizedBytes := int64(len(body))
	firstControl := -1
	inString := false
	escaped := false
	for i, b := range body {
		if inString && isJSONControlByte(b) {
			if normalizedBytes > maxNormalizedBytes-5 {
				return nil, &http.MaxBytesError{Limit: maxNormalizedBytes}
			}
			normalizedBytes += 5
			if firstControl < 0 {
				firstControl = i
			}
			escaped = false
			continue
		}

		switch {
		case escaped:
			escaped = false
		case inString && b == '\\':
			escaped = true
		case b == '"':
			inString = !inString
		}
	}
	if beforeNormalize != nil {
		if err := beforeNormalize(int(normalizedBytes)); err != nil {
			return nil, err
		}
	}
	if firstControl < 0 {
		return body, nil
	}
	out := make([]byte, 0, int(normalizedBytes))
	out = append(out, body[:firstControl]...)
	inString, escaped = true, false
	for _, b := range body[firstControl:] {
		if inString && isJSONControlByte(b) {
			out = appendJSONUnicodeEscape(out, b)
			escaped = false
			continue
		}
		switch {
		case escaped:
			escaped = false
		case inString && b == '\\':
			escaped = true
		case b == '"':
			inString = !inString
		}
		out = append(out, b)
	}
	return out, nil
}

func trimUTF8BOM(body []byte) []byte {
	if len(body) >= jsonUTF8BOMLen && body[0] == 0xef && body[1] == 0xbb && body[2] == 0xbf {
		return body[jsonUTF8BOMLen:]
	}
	return body
}

func isJSONControlByte(b byte) bool {
	return b < 0x20 || b == 0x7f
}

func appendJSONUnicodeEscape(dst []byte, b byte) []byte {
	const hex = "0123456789abcdef"
	return append(dst, '\\', 'u', '0', '0', hex[b>>4], hex[b&0x0f])
}
