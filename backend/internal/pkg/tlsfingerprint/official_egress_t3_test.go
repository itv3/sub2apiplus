package tlsfingerprint

import (
	"crypto/x509"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sort"
	"strings"
	"testing"

	utls "github.com/refraction-networking/utls"
	"github.com/stretchr/testify/require"
)

func TestOfficialEgressT3_RandomizedExtensionsKeepSetButChangeOrder(t *testing.T) {
	profile := &Profile{
		Extensions:          []uint16{0, 5, 10, 11, 13, 23, 35, 43, 45, 51},
		RandomizeExtensions: true,
	}
	orders := make(map[string]struct{})
	var expectedSet string
	for i := 0; i < 32; i++ {
		spec := buildClientHelloSpecFromProfile(profile)
		types := make([]string, 0, len(spec.Extensions))
		for _, extension := range spec.Extensions {
			types = append(types, fmt.Sprintf("%T", extension))
		}
		orders[strings.Join(types, ",")] = struct{}{}
		sortedTypes := append([]string(nil), types...)
		sort.Strings(sortedTypes)
		currentSet := strings.Join(sortedTypes, ",")
		if expectedSet == "" {
			expectedSet = currentSet
		}
		require.Equal(t, expectedSet, currentSet)
	}
	require.Greater(t, len(orders), 1, "扩展顺序应按握手变化，不能固定单一 JA3")
}

func TestOfficialEgressT3_CustomRootCAsAreUsedByUTLSDialer(t *testing.T) {
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))
	t.Cleanup(server.Close)

	rootCAs := x509.NewCertPool()
	rootCAs.AddCert(server.Certificate())
	profile := &Profile{
		CipherSuites:        []uint16{0x1301, 0x1302, 0x1303, 0xc02f},
		Curves:              []uint16{0x001d, 0x0017},
		SignatureAlgorithms: []uint16{0x0403, 0x0804, 0x0401},
		SupportedVersions:   []uint16{utls.VersionTLS13, utls.VersionTLS12},
		KeyShareGroups:      []uint16{0x001d},
		PSKModes:            []uint16{1},
		Extensions:          []uint16{0, 10, 13, 43, 45, 51},
		TLSVersMin:          uint16(utls.VersionTLS12),
		TLSVersMax:          uint16(utls.VersionTLS13),
		RootCAs:             rootCAs,
	}
	transport := &http.Transport{
		DialTLSContext: NewDialer(profile, nil).DialTLSContext,
	}
	response, err := (&http.Client{Transport: transport}).Get(server.URL)
	require.NoError(t, err)
	require.Equal(t, http.StatusNoContent, response.StatusCode)
	require.NoError(t, response.Body.Close())
}
