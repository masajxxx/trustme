from __future__ import annotations

import datetime
import ipaddress
import os
import ssl
from base64 import urlsafe_b64encode
from contextlib import contextmanager
from enum import Enum
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING, Generator, List, Optional, Union

import idna
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    load_pem_private_key,
)
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from ._version import __version__

if TYPE_CHECKING:  # pragma: no cover
    import OpenSSL.SSL

    CERTIFICATE_PUBLIC_KEY_TYPES = Union[rsa.RSAPublicKey, ec.EllipticCurvePublicKey]
    CERTIFICATE_PRIVATE_KEY_TYPES = Union[rsa.RSAPrivateKey, ec.EllipticCurvePrivateKey]

__all__ = ["CA"]

# Default certificate expiry date:
# OpenSSL on Windows fails if you try to give it a date after
# ~3001-01-19:
#   https://github.com/pyca/cryptography/issues/3194
DEFAULT_EXPIRY = datetime.datetime(3000, 1, 1)
DEFAULT_NOT_BEFORE = datetime.datetime(2000, 1, 1)


def _name(
    name: str,
    organization_name: Optional[str] = None,
    common_name: Optional[str] = None,
) -> x509.Name:
    name_pieces = [
        x509.NameAttribute(
            NameOID.ORGANIZATION_NAME,
            organization_name or f"trustme v{__version__}",
        ),
        x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, name),
    ]
    if common_name is not None:
        name_pieces.append(x509.NameAttribute(NameOID.COMMON_NAME, common_name))
    return x509.Name(name_pieces)


def random_text() -> str:
    return urlsafe_b64encode(os.urandom(12)).decode("ascii")


def _smells_like_pyopenssl(ctx: object) -> bool:
    return getattr(ctx, "__module__", "").startswith("OpenSSL")  # type: ignore[no-any-return]


def _cert_builder_common(
    subject: x509.Name,
    issuer: x509.Name,
    public_key: CERTIFICATE_PUBLIC_KEY_TYPES,
    not_after: Optional[datetime.datetime] = None,
    not_before: Optional[datetime.datetime] = None,
) -> x509.CertificateBuilder:
    not_after = not_after if not_after else DEFAULT_EXPIRY
    not_before = not_before if not_before else DEFAULT_NOT_BEFORE
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(public_key)
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .serial_number(x509.random_serial_number())
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(public_key),
            critical=False,
        )
    )


def _identity_string_to_x509(identity: str) -> x509.GeneralName:
    # Because we are a DWIM library for lazy slackers, we cheerfully pervert
    # the cryptography library's carefully type-safe API, and silently DTRT
    # for any of the following identity types:
    #
    # - "example.org"
    # - "example.org"
    # - "éxamplë.org"
    # - "xn--xampl-9rat.org"
    # - "xn--xampl-9rat.org"
    # - "127.0.0.1"
    # - "::1"
    # - "10.0.0.0/8"
    # - "2001::/16"
    # - "example@example.org"
    #
    # plus wildcard variants of the identities.
    if not isinstance(identity, str):
        raise TypeError("identities must be str")

    if "@" in identity:
        return x509.RFC822Name(identity)

    # Have to try ip_address first, because ip_network("127.0.0.1") is
    # interpreted as being the network 127.0.0.1/32. Which I guess would be
    # fine, actually, but why risk it.
    try:
        return x509.IPAddress(ipaddress.ip_address(identity))
    except ValueError:
        try:
            return x509.IPAddress(ipaddress.ip_network(identity))
        except ValueError:
            pass

    # Encode to an A-label, like cryptography wants
    if identity.startswith("*."):
        alabel_bytes = b"*." + idna.encode(identity[2:], uts46=True)
    else:
        alabel_bytes = idna.encode(identity, uts46=True)
    # Then back to text, which is mandatory on cryptography 2.0 and earlier,
    # and may or may not be deprecated in cryptography 2.1.
    alabel = alabel_bytes.decode("ascii")
    return x509.DNSName(alabel)


class Blob:
    """A convenience wrapper for a blob of bytes.

    This type has no public constructor. They're used to provide a handy
    interface to the PEM-encoded data generated by `trustme`. For example, see
    `CA.cert_pem` or `LeafCert.private_key_and_cert_chain_pem`.

    """

    def __init__(self, data: bytes) -> None:
        self._data = data

    def bytes(self) -> bytes:
        """Returns the data as a `bytes` object."""
        return self._data

    def write_to_path(
        self, path: Union[str, "os.PathLike[str]"], append: bool = False
    ) -> None:
        """Writes the data to the file at the given path.

        Args:
          path: The path to write to.
          append: If False (the default), replace any existing file
               with the given name. If True, append to any existing file.

        """
        if append:
            mode = "ab"
        else:
            mode = "wb"
        with open(path, mode) as f:
            f.write(self._data)

    @contextmanager
    def tempfile(self, dir: Optional[str] = None) -> Generator[str, None, None]:
        """Context manager for writing data to a temporary file.

        The file is created when you enter the context manager, and
        automatically deleted when the context manager exits.

        Many libraries have annoying APIs which require that certificates be
        specified as filesystem paths, so even if you have already the data in
        memory, you have to write it out to disk and then let them read it
        back in again. If you encounter such a library, you should probably
        file a bug. But in the mean time, this context manager makes it easy
        to give them what they want.

        Example:

          Here's how to get requests to use a trustme CA (`see also
          <http://docs.python-requests.org/en/master/user/advanced/#ssl-cert-verification>`__)::

           ca = trustme.CA()
           with ca.cert_pem.tempfile() as ca_cert_path:
               requests.get("https://localhost/...", verify=ca_cert_path)

        Args:
          dir: Passed to `tempfile.NamedTemporaryFile`.

        """
        # On Windows, you can't re-open a NamedTemporaryFile that's still
        # open. Which seems like it completely defeats the purpose of having a
        # NamedTemporaryFile? Oh well...
        # https://bugs.python.org/issue14243
        f = NamedTemporaryFile(suffix=".pem", dir=dir, delete=False)
        try:
            f.write(self._data)
            f.close()
            yield f.name
        finally:
            f.close()  # in case write() raised an error
            os.unlink(f.name)


class KeyType(Enum):
    """Type of the key used to generate a certificate"""

    RSA = 0
    ECDSA = 1

    def _generate_key(self) -> CERTIFICATE_PRIVATE_KEY_TYPES:
        if self is KeyType.RSA:
            # key_size needs to be a least 2048 to be accepted
            # on Debian and pressumably other OSes

            return rsa.generate_private_key(public_exponent=65537, key_size=2048)
        elif self is KeyType.ECDSA:
            return ec.generate_private_key(ec.SECP256R1())
        else:  # pragma: no cover
            raise ValueError("Unknown key type")


class CA:
    """A certificate authority."""

    _certificate: x509.Certificate

    def __init__(
        self,
        parent_cert: Optional[CA] = None,
        path_length: int = 9,
        organization_name: Optional[str] = None,
        organization_unit_name: Optional[str] = None,
        key_type: KeyType = KeyType.ECDSA,
    ) -> None:
        self.parent_cert = parent_cert
        self._private_key = key_type._generate_key()
        self._path_length = path_length

        name = _name(
            organization_unit_name or "Testing CA #" + random_text(),
            organization_name=organization_name,
        )
        issuer = name
        sign_key = self._private_key
        aki: Optional[x509.AuthorityKeyIdentifier]
        if parent_cert is not None:
            sign_key = parent_cert._private_key
            parent_certificate = parent_cert._certificate
            issuer = parent_certificate.subject
            ski_ext = parent_certificate.extensions.get_extension_for_class(
                x509.SubjectKeyIdentifier
            )
            aki = x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(
                ski_ext.value
            )
        else:
            aki = None
        cert_builder = _cert_builder_common(
            name, issuer, self._private_key.public_key()
        ).add_extension(
            x509.BasicConstraints(ca=True, path_length=path_length),
            critical=True,
        )
        if aki:
            cert_builder = cert_builder.add_extension(aki, critical=False)
        self._certificate = cert_builder.add_extension(
            x509.KeyUsage(
                digital_signature=True,  # OCSP
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,  # sign certs
                crl_sign=True,  # sign revocation lists
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        ).sign(
            private_key=sign_key,
            algorithm=hashes.SHA256(),
        )

    @property
    def cert_pem(self) -> Blob:
        """`Blob`: The PEM-encoded certificate for this CA. Add this to your
        trust store to trust this CA."""
        return Blob(self._certificate.public_bytes(Encoding.PEM))

    @property
    def private_key_pem(self) -> Blob:
        """`Blob`: The PEM-encoded private key for this CA. Use this to sign
        other certificates from this CA."""
        return Blob(
            self._private_key.private_bytes(
                Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption()
            )
        )

    def create_child_ca(self, key_type: KeyType = KeyType.ECDSA) -> "CA":
        """Creates a child certificate authority

        Returns:
          CA: the newly-generated certificate authority

        Raises:
          ValueError: if the CA path length is 0
        """
        if self._path_length == 0:
            raise ValueError("Can't create child CA: path length is 0")

        path_length = self._path_length - 1
        return CA(parent_cert=self, path_length=path_length, key_type=key_type)

    def issue_cert(
        self,
        *identities: str,
        common_name: Optional[str] = None,
        organization_name: Optional[str] = None,
        organization_unit_name: Optional[str] = None,
        not_before: Optional[datetime.datetime] = None,
        not_after: Optional[datetime.datetime] = None,
        key_type: KeyType = KeyType.ECDSA,
    ) -> "LeafCert":
        """Issues a certificate. The certificate can be used for either servers
        or clients.

        Args:
          identities: The identities that this certificate will be valid for.
            Most commonly, these are just hostnames, but we accept any of the
            following forms:

            - Regular hostname: ``example.com``
            - Wildcard hostname: ``*.example.com``
            - International Domain Name (IDN): ``café.example.com``
            - IDN in A-label form: ``xn--caf-dma.example.com``
            - IPv4 address: ``127.0.0.1``
            - IPv6 address: ``::1``
            - IPv4 network: ``10.0.0.0/8``
            - IPv6 network: ``2001::/16``
            - Email address: ``example@example.com``

            These ultimately end up as "Subject Alternative Names", which are
            what modern programs are supposed to use when checking identity.

          common_name: Sets the "Common Name" of the certificate. This is a
            legacy field that used to be used to check identity. It's an
            arbitrary string with poorly-defined semantics, so `modern
            programs are supposed to ignore it
            <https://developers.google.com/web/updates/2017/03/chrome-58-deprecations#remove_support_for_commonname_matching_in_certificates>`__.
            But it might be useful if you need to test how your software
            handles legacy or buggy certificates.

          organization_name: Sets the "Organization Name" (O) attribute on the
            certificate. By default, it will be "trustme" suffixed with a
            version number.

          organization_unit_name: Sets the "Organization Unit Name" (OU)
            attribute on the certificate. By default, a random one will be
            generated.

          not_before: Set the validity start date (notBefore) of the certificate.
            This argument type is `datetime.datetime`.

          not_after: Set the expiry date (notAfter) of the certificate. This
            argument type is `datetime.datetime`.

          key_type: Set the type of key that is used for the certificate. By default this is an ECDSA based key.

        Returns:
          LeafCert: the newly-generated certificate.

        """
        if not identities and common_name is None:
            raise ValueError("Must specify at least one identity or common name")

        key = key_type._generate_key()

        ski_ext = self._certificate.extensions.get_extension_for_class(
            x509.SubjectKeyIdentifier
        )
        aki = x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(
            ski_ext.value
        )

        cert = (
            _cert_builder_common(
                _name(
                    organization_unit_name or "Testing cert #" + random_text(),
                    organization_name=organization_name,
                    common_name=common_name,
                ),
                self._certificate.subject,
                key.public_key(),
                not_before=not_before,
                not_after=not_after,
            )
            .add_extension(
                x509.BasicConstraints(ca=False, path_length=None),
                critical=True,
            )
            .add_extension(aki, critical=False)
            .add_extension(
                x509.SubjectAlternativeName(
                    [_identity_string_to_x509(ident) for ident in identities]
                ),
                critical=True,
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=True,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=False,
                    crl_sign=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.ExtendedKeyUsage(
                    [
                        ExtendedKeyUsageOID.CLIENT_AUTH,
                        ExtendedKeyUsageOID.SERVER_AUTH,
                        ExtendedKeyUsageOID.CODE_SIGNING,
                    ]
                ),
                critical=True,
            )
            .sign(
                private_key=self._private_key,
                algorithm=hashes.SHA256(),
            )
        )

        chain_to_ca = []
        ca = self
        while ca.parent_cert is not None:
            chain_to_ca.append(ca._certificate.public_bytes(Encoding.PEM))
            ca = ca.parent_cert

        return LeafCert(
            key.private_bytes(
                Encoding.PEM,
                PrivateFormat.TraditionalOpenSSL,
                NoEncryption(),
            ),
            cert.public_bytes(Encoding.PEM),
            chain_to_ca,
        )

    # For backwards compatibility
    issue_server_cert = issue_cert

    def configure_trust(self, ctx: Union[ssl.SSLContext, OpenSSL.SSL.Context]) -> None:
        """Configure the given context object to trust certificates signed by
        this CA.

        Args:
          ctx: The SSL context to be modified.

        """
        if isinstance(ctx, ssl.SSLContext):
            ctx.load_verify_locations(cadata=self.cert_pem.bytes().decode("ascii"))
        elif _smells_like_pyopenssl(ctx):
            from OpenSSL import crypto

            cert = crypto.load_certificate(crypto.FILETYPE_PEM, self.cert_pem.bytes())
            store = ctx.get_cert_store()
            store.add_cert(cert)
        else:
            raise TypeError(
                "unrecognized context type {!r}".format(ctx.__class__.__name__)
            )

    @classmethod
    def from_pem(cls, cert_bytes: bytes, private_key_bytes: bytes) -> "CA":
        """Build a CA from existing cert and private key.

        This is useful if your test suite has an existing certificate authority and
        you're not ready to switch completely to trustme just yet.

        Args:
          cert_bytes: The bytes of the certificate in PEM format
          private_key_bytes: The bytes of the private key in PEM format
        """
        ca = cls()
        ca.parent_cert = None
        ca._certificate = x509.load_pem_x509_certificate(cert_bytes)
        ca._private_key = load_pem_private_key(private_key_bytes, password=None)  # type: ignore[assignment]

        return ca


class LeafCert:
    """A server or client certificate.

    This type has no public constructor; you get one by calling
    `CA.issue_cert` or similar.

    Attributes:
      private_key_pem (`Blob`): The PEM-encoded private key corresponding to
          this certificate.

      cert_chain_pems (list of `Blob` objects): The zeroth entry in this list
          is the actual PEM-encoded certificate, and any entries after that
          are the rest of the certificate chain needed to reach the root CA.

      private_key_and_cert_chain_pem (`Blob`): A single `Blob` containing the
          concatenation of the PEM-encoded private key and the PEM-encoded
          cert chain.

    """

    def __init__(
        self, private_key_pem: bytes, server_cert_pem: bytes, chain_to_ca: List[bytes]
    ) -> None:
        self.private_key_pem = Blob(private_key_pem)
        self.cert_chain_pems = [Blob(pem) for pem in [server_cert_pem] + chain_to_ca]
        self.private_key_and_cert_chain_pem = Blob(
            private_key_pem + server_cert_pem + b"".join(chain_to_ca)
        )

    def configure_cert(self, ctx: Union[ssl.SSLContext, OpenSSL.SSL.Context]) -> None:
        """Configure the given context object to present this certificate.

        Args:
          ctx: The SSL context to be modified.

        """
        if isinstance(ctx, ssl.SSLContext):
            # Currently need a temporary file for this, see:
            #   https://bugs.python.org/issue16487
            with self.private_key_and_cert_chain_pem.tempfile() as path:
                ctx.load_cert_chain(path)
        elif _smells_like_pyopenssl(ctx):
            from OpenSSL.crypto import FILETYPE_PEM, load_certificate, load_privatekey

            key = load_privatekey(FILETYPE_PEM, self.private_key_pem.bytes())
            ctx.use_privatekey(key)
            cert = load_certificate(FILETYPE_PEM, self.cert_chain_pems[0].bytes())
            ctx.use_certificate(cert)
            for pem in self.cert_chain_pems[1:]:
                cert = load_certificate(FILETYPE_PEM, pem.bytes())
                ctx.add_extra_chain_cert(cert)
        else:
            raise TypeError(
                "unrecognized context type {!r}".format(ctx.__class__.__name__)
            )
