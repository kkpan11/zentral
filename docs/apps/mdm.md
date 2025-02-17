# MDM

Zentral can be used as MDM server for Apple devices.

## Zentral configuration

To activate the MDM module, you need to add a `zentral.contrib.mdm` section to the `apps` section in `base.json`.

### SCEP CA issuer chain

To authenticate the OTA enrollments, Zentral needs the SCEP CA issuer certificate chain in PEM form in the `scep_ca_fullchain` key of the `zentral.contrib.mdm` section. It is possible to use the `{{ file:PATH_TO_PEM_CHAIN }}` substitution to load the chain from a file on disk.

### mTLS proxy

Zentral is expecting the client certificate in PEM form in the `X-SSL-Client-Cert` header, and the client certificate subject DN in the `X-SSL-Client-S-DN` header. If this is not possible, you can set `mtls_proxy` to `false` in the `zentral.contrib.mdm` section. In that case, the Apple devices will be configured to add a header containing the payload signature in each HTTP request. See the [Apple documentation](https://developer.apple.com/documentation/devicemanagement/implementing_device_management/managing_certificates_for_mdm_servers_and_devices#3677960). This adds approximately 2KB of data to each message.

## Push certificates

To be able to send notifications to the devices, Zentral needs a push certificate (aka. APNS certificate). To get one, you first need to generate an MDM vendor certificate. An Apple [Developer Enterprise Account](https://developer.apple.com/programs/enterprise/) with the ability to generate MDM CSRs is required. You can then use this vendor certificate to sign an APNS certificate request. The `mdmcerts` Zentral management command can be used to help with this process.

### MDM vendor certificate

Run the following command to setup a working directory with a vendor certificate request:

```bash
python server/manage.py mdmcerts -d the_working_directory init
```

* Choose a password for the vendor certificate request private key, and remember it!

The content of the working directory should be the following:
```bash
$ ls the_working_directory
vendor.csr  vendor.key
```

 * Sign in to the [Apple Developer Portal](https://developer.apple.com/account) and navigate to [Certificates, Identifiers & Profiles](https://developer.apple.com/account/resources/certificates/list).
 * Create a new certificate, choose *Services > MDM CSR*.
 * Upload the `vendor.csr` file.
 * Download the generated certificate and store it as `vendor.crt` in the working directory.

### Push/APNS certificate

Run the following command to create an APNS certificate request and sign it with the vendor certificate:

```bash
python server/manage.py mdmcerts -d the_working_directory req COUNTRYCODE
```

 * Choose a password for the push/APNS certificate request private key, and remember it!
 * Enter the password for the MDM vendor certificate private key.

The content of the working directory should be the following:
```bash
$ ls the_working_directory
push.b64  push.csr  push.key  vendor.crt  vendor.csr  vendor.key
```

 * Sign in to the [Apple Push Certificate Portal](https://identity.apple.com).
 * To renew an existing certificate, choose the certificate and click the *Renew* button.
 * To create a new certificate, click the *Create a Certificate* button.
 * Upload the `push.b64` signed certificate request.
 * Download the generated certificate.

Navigate to the Zentral *MDM > Push certificates* section, and either select an existing certificate and click on the *Update* button to renew an existing certificate, or click on the *Add* button to create a new push certificate. Upload the generated certificate, the `push.key` private key, and enter the password of the push certificate private key.

### Renewing a Push/APNS certificate

**IMPORTANT** do not let the push/APNS certificates expire! Remember to renew them ahead of their expiry!

To be able to keep sending notifications to enrolled devices, it is important to renew the existing certificates, and not generate new ones (it it important that the *topic* of a push certificate stays the same). In the [Apple Push Certificate Portal](https://identity.apple.com), look for the existing certificate and click on the `Renew` button, and not on the `Create a Certificate` button. In the Zentral *MDM > Push certificates* section, find the certificate and click on the *Update* button, and do not *Add* a new certificate.

## HTTP API

### `/api/mdm/dep/virtual_servers/<int:pk>/sync_devices/`

 * method: `POST`
 * required_permission: `mdm.view_depvirtualserver`

Use this endpoint to trigger a DEP virtual server devices sync.

Example:

```bash
curl -XPOST \
  -H "Authorization: Token $ZTL_API_TOKEN" \
  https://$ZTL_FQDN/api/mdm/dep/virtual_servers/1/sync_devices/ \
  | python3 -m json.tool
```

Response:

```json
{
  "task_id": "b1512b8d-1e17-4181-a1c3-93a7243fddd4",
  "task_result_url": "/api/task_result/b1512b8d-1e17-4181-a1c3-93a7243fddd4/"
}
```

### `/api/mdm/software_updates/sync/`

 * method: `POST`
 * required_permission:
    * `mdm.add_softwareupdate`
    * `mdm.change_softwareupdate`
    * `mdm.delete_softwareupdate`

Use this endpoint to trigger a Software Updates sync.

Example:

```bash
curl -XPOST \
  -H "Authorization: Token $ZTL_API_TOKEN" \
  https://$ZTL_FQDN/api/mdm/software_updates/sync/ \
  | python3 -m json.tool
```

Response:

```json
{
  "task_id": "b1512b8d-1e17-4181-a1c3-93a7243fddd4",
  "task_result_url": "/api/task_result/b1512b8d-1e17-4181-a1c3-93a7243fddd4/"
}
```
