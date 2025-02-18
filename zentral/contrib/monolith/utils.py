from datetime import datetime
import hashlib
import logging
import plistlib
from django.core.files.base import ContentFile
from zentral.utils.payloads import generate_payload_uuid, get_payload_identifier
from zentral.utils.osx_package import get_tls_hostname
from zentral.utils.text import shard as compute_shard


logger = logging.getLogger('zentral.contrib.monolith.utils')


# special munki catalogs and packages for zentral enrollment


def make_package_info(builder, manifest_enrollment_package, package_content):
    h = hashlib.sha256(package_content)
    installer_item_hash = h.hexdigest()
    installer_item_size = len(package_content)
    installed_size = installer_item_size * 10  # TODO: bug
    installcheck_script = (
        '#!/usr/local/munki/munki-python\n'
        'import plistlib\n'
        'import sys\n'
        '\n'
        '\n'
        'def do_install_check():\n'
        f'    with open("/usr/local/zentral/{builder.local_subfolder}/enrollment.plist", "rb") as f:\n'
        '        info = plistlib.load(f)\n'
        '    return (\n'
        f'        info["enrollment"]["id"] == {builder.enrollment.pk}\n'
        f'        and info["enrollment"]["version"] == {builder.enrollment.version}\n'
        f'        and info["fqdn"] == "{builder.get_tls_hostname()}"\n'
        '    )\n'
        '\n'
        '\n'
        'if __name__ == "__main__":\n'
        '    try:\n'
        '        ok = do_install_check()\n'
        '    except Exception:\n'
        '        pass\n'
        '    else:\n'
        '        if ok:\n'
        '            sys.exit(1)\n'
    )
    return {'description': '{} package'.format(builder.name),
            'display_name': builder.name,
            'installed_size': installed_size,
            'installer_item_hash': installer_item_hash,
            'installer_item_size': installer_item_size,
            'minimum_os_version': '10.9.0',  # TODO: hardcoded
            'name': manifest_enrollment_package.get_name(),
            'installcheck_script': installcheck_script,
            'receipts': [
                {'installed_size': installed_size,
                 'packageid': builder.package_identifier,
                 'version': builder.package_version},
            ],
            'unattended_install': True,
            'unattended_uninstall': True,
            'uninstallable': True,
            'uninstall_method': 'removepackages',
            'requires': manifest_enrollment_package.get_requires(),
            'version': builder.package_version}


def build_manifest_enrollment_package(mep):
    builder = mep.builder_class(mep.get_enrollment(), version=mep.version)
    _, _, package_content = builder.build()
    mep.pkg_info = make_package_info(builder, mep, package_content)
    mep.file.delete(False)
    mep.file.save(mep.get_installer_item_filename(),
                  ContentFile(package_content),
                  save=True)


def build_configuration(enrollment):
    # TODO: hardcoded
    config = {
        "ClientIdentifier": "$SERIALNUMBER",
        "SoftwareRepoURL": "https://{}/public/monolith/munki_repo".format(get_tls_hostname()),
        "FollowHTTPRedirects": "all",
        # "ManifestURL": None,  # no special Manifest URL with monolith
        # force redirect via monolith for Icon and Client Resource
        # "IconURL": None,
        # "ClientResourceURL": None,
        "AdditionalHttpHeaders": [
            "Authorization: Bearer {}".format(enrollment.secret.secret),
            "X-Zentral-Serial-Number: $SERIALNUMBER",
            "X-Zentral-UUID: $UDID",
        ],
    }
    return config


def build_configuration_plist(enrollment):
    content = plistlib.dumps(build_configuration(enrollment))
    return f"zentral_monolith_configuration.enrollment_{enrollment.pk}.plist", content


def build_configuration_profile(enrollment):
    payload_content = {"PayloadContent": {"ManagedInstalls": {"Forced": [
                           {"mcx_preference_settings": build_configuration(enrollment)}
                       ]}},
                       "PayloadEnabled": True,
                       "PayloadIdentifier": get_payload_identifier("monolith.settings.0"),
                       "PayloadUUID": generate_payload_uuid(),
                       "PayloadType": "com.apple.ManagedClient.preferences",
                       "PayloadVersion": 1}
    configuration_profile_data = {"PayloadContent": [payload_content],
                                  "PayloadDescription": "Munki settings for Zentral/Monolith",
                                  "PayloadDisplayName": "Zentral - Munki settings",
                                  "PayloadIdentifier": get_payload_identifier("monolith.settings"),
                                  "PayloadOrganization": "Zentral",
                                  "PayloadRemovalDisallowed": True,
                                  "PayloadScope": "System",
                                  "PayloadType": "Configuration",
                                  "PayloadUUID": generate_payload_uuid(),
                                  "PayloadVersion": 1}
    content = plistlib.dumps(configuration_profile_data)
    return f"zentral_monolith_configuration.enrollment_{enrollment.pk}.mobileconfig", content


def test_monolith_object_inclusion(key, options, serial_number, tag_names):
    shard = 100
    modulo = 100
    if options:
        excluded_tag_names = options.get("excluded_tags")
        if excluded_tag_names and any(etn in tag_names for etn in excluded_tag_names):
            # one excluded tag match, skip
            return False
        # not excluded, evaluate the shard
        shards = options.get("shards")
        if shards:
            modulo = shards.get("modulo", 100)
            shard = default = shards.get("default", modulo)
            tag_shards = shards.get("tags")
            if tag_shards:
                try:
                    shard = max(tag_shards[tn] for tn in tag_names if tn in tag_shards)
                except ValueError:
                    # no tag match
                    shard = default
    return (
        shard >= modulo or
        compute_shard(key + serial_number, modulo=modulo) < shard
    )


def test_pkginfo_catalog_inclusion(pkginfo, serial_number, tag_names):
    return test_monolith_object_inclusion(
        pkginfo["name"] + pkginfo["version"],
        pkginfo.get("zentral_monolith"),
        serial_number,
        tag_names
    )


def filter_catalog_data(catalog_data, serial_number, tag_names):
    filtered_catalog_data = []
    for pkginfo in catalog_data:
        if test_pkginfo_catalog_inclusion(pkginfo, serial_number, tag_names):
            force_install_after_date = pkginfo.get("force_install_after_date")
            if isinstance(force_install_after_date, str):
                pkginfo["force_install_after_date"] = datetime.fromisoformat(force_install_after_date)
            filtered_catalog_data.append(pkginfo)
    return filtered_catalog_data


def filter_sub_manifest_data_dict(smd, serial_number, tag_names):
    for key in ('managed_installs', 'optional_installs'):
        if key not in smd:
            continue
        smd[key] = [
            name
            for name, options in smd.pop(key)
            if test_monolith_object_inclusion(name, options, serial_number, tag_names)
        ]


def filter_sub_manifest_data(sub_manifest_data, serial_number, tag_names):
    filter_sub_manifest_data_dict(sub_manifest_data, serial_number, tag_names)
    for condition_d in sub_manifest_data.get("conditional_items", []):
        filter_sub_manifest_data_dict(condition_d, serial_number, tag_names)
    return sub_manifest_data
