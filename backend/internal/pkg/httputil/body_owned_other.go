//go:build !linux && !darwin

package httputil

func mapOwnedRequestBody(size int) ([]byte, bool, error) {
	return make([]byte, size), false, nil
}

func unmapOwnedRequestBody([]byte) error { return nil }
