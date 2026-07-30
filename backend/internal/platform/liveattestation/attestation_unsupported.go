//go:build !darwin && !candidatecapture

package liveattestation

func NewProvider() Provider {
	return unsupportedProvider{}
}
