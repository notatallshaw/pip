(SSL Certificate Verification)=

# HTTPS Certificates

```{versionadded} 1.3

```

By default, pip will perform SSL certificate verification for network
connections it makes over HTTPS. These serve to prevent man-in-the-middle
attacks against package downloads.

## Using a specific certificate store

The `--cert` option (and the corresponding `PIP_CERT` environment variable)
allows users to specify a different certificate source for pip to use. The
source can be a PEM-encoded CA bundle or an OpenSSL-compatible certificate
directory. Directory entries must use OpenSSL's hash naming, which can be
generated with `openssl rehash`.

A custom CA source replaces pip's default roots for HTTPS origin verification
and uses OpenSSL verification on supported platforms. Pip's `cert`
configuration takes precedence over the `REQUESTS_CA_BUNDLE` and
`CURL_CA_BUNDLE` compatibility environment variables; `REQUESTS_CA_BUNDLE`
takes precedence over `CURL_CA_BUNDLE`.

If you need a specific certificate bundle, you can download the
[Mozilla CA bundle provided by the curl project](https://curl.se/docs/caextract.html).

## Using system certificate stores

```{versionadded} 24.2

```

```{note}
Versions of pip prior to v24.2 did not use system certificates by default.
To use system certificates with pip v22.2 or later, you must opt-in using the `--use-feature=truststore` CLI flag.
```

By default
system certificates are used in addition to certifi to verify HTTPS connections.
This functionality is provided through the {pypi}`truststore` package.

If you encounter a TLS/SSL error when using the `truststore` feature you should
open an issue on the [truststore GitHub issue tracker] instead of pip's issue
tracker. The maintainers of truststore will help diagnose and fix the issue.

To opt-out of using system certificates you can pass the `--use-deprecated=legacy-certs`
flag to pip.

[truststore github issue tracker]:
  https://github.com/sethmlarson/truststore/issues
