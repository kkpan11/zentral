import base64
import os
import subprocess
from tempfile import NamedTemporaryFile
from asn1crypto import cms
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.x509.oid import NameOID
from django.utils.crypto import get_random_string
from django.utils.functional import SimpleLazyObject
from zentral.conf import settings
from OpenSSL import crypto


# CA verification


IPHONE_DEVICE_CA_RFC_4514 = 'CN=Apple iPhone Device CA,OU=Apple iPhone,O=Apple Inc.,C=US'
IPHONE_DEVICE_CA_FULLCHAIN = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "assets",
    "Apple_iPhone_Device_CA_Fullchain.pem"
)


def verify_store_certificate(store, certificate_bytes):
    certificate = crypto.load_certificate(crypto.FILETYPE_ASN1, certificate_bytes)
    store_ctx = crypto.X509StoreContext(store, certificate)
    try:
        store_ctx.verify_certificate()
    except crypto.X509StoreContextError:
        return False
    else:
        return True


def get_scep_ca_store():
    store = crypto.X509Store()
    head = "-----BEGIN CERTIFICATE-----"
    for tail in settings["apps"]["zentral.contrib.mdm"]["scep_ca_fullchain"].split(head)[1:]:
        certificate_bytes = (head + tail).encode("utf-8")
        store.add_cert(crypto.load_certificate(crypto.FILETYPE_PEM, certificate_bytes))
    return store


SCEP_CA_STORE = SimpleLazyObject(get_scep_ca_store)


def verify_zentral_scep_ca_issuer(certificate_bytes):
    return verify_store_certificate(SCEP_CA_STORE, certificate_bytes)


def get_iphone_device_ca_store():
    store = crypto.X509Store()
    store.load_locations(IPHONE_DEVICE_CA_FULLCHAIN)
    store.set_flags(0x200000)  # TODO hack! see X509_V_FLAG_NO_CHECK_TIME, because one of the cert has expired!
    return store


IPHONE_DEVICE_CA_STORE = SimpleLazyObject(get_iphone_device_ca_store)


def verify_apple_iphone_device_ca_issuer(certificate_bytes):
    return verify_store_certificate(IPHONE_DEVICE_CA_STORE, certificate_bytes)


# CMS / PKCS7


def get_signer_certificate(content, signer):
    certificates = content["certificates"]
    signer_id = signer["sid"]
    for certificate in certificates:
        if certificate.chosen.serial_number == signer_id.chosen["serial_number"].native and \
           certificate.chosen.issuer == signer_id.chosen["issuer"]:
            certificate_bytes = certificate.dump()
            certificate = x509.load_der_x509_certificate(certificate_bytes)
            certificate_i = certificate.issuer.rfc4514_string()
            return certificate_i, certificate_bytes, certificate


def get_cryptography_hash_algorithm(signer):
    hash_name = signer["digest_algorithm"]["algorithm"].native
    if hash_name == "sha1":
        return hashes.SHA1
    elif hash_name == "sha256":
        return hashes.SHA256
    elif hash_name == "sha512":
        return hashes.SHA512
    else:
        raise ValueError("Unknown hash {}".format(hash_name))


def verify_certificate_signature(certificate, signer, payload):
    signature = signer['signature'].native
    if "signed_attrs" in signer and signer["signed_attrs"]:
        # Seen with the iPhone simulator for example
        signed_string = signer["signed_attrs"].dump()
        if signed_string.startswith(b'\xa0'):
            # TODO: WTF!!!
            # see https://stackoverflow.com/questions/24567623/how-to-see-what-attributes-are-signed-inside-pkcs7#24581628  # NOQA
            signed_string = b'\x31' + signed_string[1:]
    else:
        signed_string = payload
    public_key = certificate.public_key()
    signature_algo = signer["signature_algorithm"].signature_algo
    hash_algorithm = get_cryptography_hash_algorithm(signer)
    if signature_algo == "rsassa_pkcs1v15":
        try:
            public_key.verify(signature, signed_string,
                              padding.PKCS1v15(), hash_algorithm())
        except InvalidSignature:
            return False
    elif signature_algo == "ecdsa":
        try:
            public_key.verify(signature, signed_string,
                              ec.ECDSA(hash_algorithm()))
        except InvalidSignature:
            return False
    else:
        raise ValueError(f"Unknown signature_algo {signature_algo}")
    return True


def verify_signed_payload(payload, detached_signature=None):
    if detached_signature:
        content_info = cms.ContentInfo.load(detached_signature)
    else:
        content_info = cms.ContentInfo.load(payload)
    if content_info["content_type"].native != "signed_data":
        raise ValueError("Not signed data")
    content = content_info["content"]
    if not detached_signature:
        payload = content['encap_content_info']['content'].native
    certificates = []
    for signer in content["signer_infos"]:
        certificate_i, certificate_bytes, certificate = get_signer_certificate(content, signer)
        if not verify_certificate_signature(certificate, signer, payload):
            raise ValueError("Invalid signature")
        certificates.append((certificate_i, certificate_bytes, certificate))
    return certificates, payload


def verify_iphone_ca_signed_payload(data):
    certificates, payload = verify_signed_payload(data)
    for certificate_i, certificate_bytes, certificate in certificates:
        if certificate_i == IPHONE_DEVICE_CA_RFC_4514 and verify_apple_iphone_device_ca_issuer(certificate_bytes):
            return payload
    raise ValueError("Untrusted CA")


def decrypt_cms_payload(payload, private_key, der=False):
    # encrypt the private key, using a temporary password
    tmp_inkey_pwd = get_random_string(length=42).encode("utf-8")
    tmp_inkey_data = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.BestAvailableEncryption(tmp_inkey_pwd)
    )
    # use the environment to pass the temporary private key password to openssl
    env = os.environ.copy()
    env_var = get_random_string(length=12)
    env[env_var] = tmp_inkey_pwd
    # write the encryped private key in a temporary file
    with NamedTemporaryFile() as tmp_inkey_file:
        tmp_inkey_file.write(tmp_inkey_data)
        tmp_inkey_file.flush()
        # decrypt the payload
        openssl_args = [
            "/usr/bin/openssl",
            "smime", "-decrypt",
            "-inkey", tmp_inkey_file.name, "-passin", f"env:{env_var}"
        ]
        if der:
            openssl_args.extend(["-inform", "der"])
        cp = subprocess.run(
            openssl_args,
            input=payload,
            capture_output=True,
            check=True,
            env=env,
        )
        return cp.stdout


def decrypt_cms_payload_with_pem_privkey(payload, privkey_bytes):
    # load the private key
    private_key = serialization.load_pem_private_key(privkey_bytes, None)
    return decrypt_cms_payload(payload, private_key)


def encrypt_cms_payload(payload, public_key_bytes, raw_output=False):
    # write the public key in a temporary file
    with NamedTemporaryFile() as tmp_pubkey_file:
        tmp_pubkey_file.write(public_key_bytes)
        tmp_pubkey_file.flush()
        # encrypt the payload
        p = subprocess.Popen(["/usr/bin/openssl", "smime",  "-encrypt", tmp_pubkey_file.name],
                             stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE)
        stdout, _ = p.communicate(payload)
        if raw_output:
            return base64.b64decode(stdout.split(b"\n\n")[1].replace(b"\n", b""))
        else:
            return stdout


# push certificate


def generate_push_certificate_key_bytes(key_size=2048):
    key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=key_size,
    )
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()
    )


def generate_push_certificate_csr_der_bytes(push_certificate):
    key = serialization.load_pem_private_key(push_certificate.get_private_key(), None)
    csr = x509.CertificateSigningRequestBuilder().subject_name(x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, f"Zentral Push Certificate {push_certificate.pk}")
    ])).sign(key, hashes.SHA256())
    return csr.public_bytes(serialization.Encoding.DER)


def load_push_certificate_and_key(cert_pem_bytes, key_pem_bytes, password=None):
    if password:
        if isinstance(password, str):
            password = password.encode("utf-8")
    else:
        password = None
    try:
        cert = x509.load_pem_x509_certificate(cert_pem_bytes)
    except Exception:
        raise ValueError("Could not load PEM certificate")
    try:
        key = serialization.load_pem_private_key(key_pem_bytes, password=password)
    except Exception:
        raise ValueError("Could not load PEM private key")
    message = b"Buffalo buffalo buffalo buffalo"
    # padding.OAEP is recommended for new applications, but
    # we only do a quick check here, and we need to be able to use
    # small private keys to speed up the tests.
    # (TODO verify <1024bit with padding.OAEP → error)
    pad = padding.PKCS1v15()
    try:
        assert key.decrypt(cert.public_key().encrypt(message, pad), pad) == message
    except Exception:
        raise ValueError("The certificate and key do not form a pair")
    try:
        topic = cert.subject.get_attributes_for_oid(NameOID.USER_ID)[0].value
    except Exception:
        raise ValueError("Could not extract the topic from the certificate subject")
    return {"certificate": cert.public_bytes(serialization.Encoding.PEM),
            "private_key": key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption()
            ),
            "not_before": cert.not_valid_before_utc,
            "not_after": cert.not_valid_after_utc,
            "topic": topic}
