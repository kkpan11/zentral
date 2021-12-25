from datetime import datetime
import json
from unittest.mock import patch
from django.urls import reverse
from django.test import TestCase, override_settings
from django.utils.crypto import get_random_string
from django.utils.text import slugify
from zentral.contrib.inventory.models import EnrollmentSecret, MachineSnapshot, MetaBusinessUnit
from zentral.contrib.osquery.compliance_checks import sync_query_compliance_check
from zentral.contrib.osquery.conf import INVENTORY_QUERY_NAME
from zentral.contrib.osquery.events import OsqueryRequestEvent, OsqueryResultEvent, OsqueryCheckStatusUpdated
from zentral.contrib.osquery.models import (Configuration,
                                            DistributedQuery, DistributedQueryMachine, DistributedQueryResult,
                                            EnrolledMachine, Enrollment,
                                            Query, Pack, PackQuery)
from zentral.core.compliance_checks.models import MachineStatus, Status


INVENTORY_QUERY_SNAPSHOT = [
    {'build': '15D21',
     'major': '10',
     'minor': '11',
     'name': 'Mac OS X',
     'patch': '3',
     'table_name': 'os_version'},
    {'computer_name': 'godzilla',
     'cpu_brand': 'Intel(R) Core(TM)2 Duo CPU T9600 @2.80GHz',
     'cpu_logical_cores': '2',
     'cpu_physical_cores': '2',
     'cpu_subtype': 'Intel 80486',
     'cpu_type': 'i486',
     'hardware_model': 'MacBookPro5,1 \u0000',  # extra space and NULL must be removed by osquery module
     'hardware_serial': '0123456789',
     'hostname': 'godzilla.box',
     'physical_memory': '8589934592',
     'table_name': 'system_info'},
    {'address': '192.168.1.123',
     'broadcast': '192.168.1.255',
     'interface': 'en1',
     'mac': '00:23:ac:a8:49:a9',
     'mask': '255.255.255.0',
     'table_name': 'network_interface'}
]

WIN_INVENTORY_QUERY_SNAPSHOT = [
    {"build": "19041",
     "major": "10",
     "minor": "0",
     "name": "Microsoft Windows 10 Enterprise Evaluation",
     "patch": "",
     "table_name": "os_version"},
    {"computer_name": "WinDev2010Eval",
     "cpu_brand": "Intel(R) Core(TM) i7-4578U CPU @ 3.00GHz",
     "cpu_logical_cores": "2",
     "cpu_physical_cores": "2",
     "cpu_subtype": "-1",
     "cpu_type": "x86_64",
     "hardware_model": "VMware Virtual Platform",
     "hardware_serial": "VMware-56 4d e4 40 34 98 81 58-e5 82 7e b7 a6 74 cc 2d",
     "hostname": "WinDev2010Eval",
     "physical_memory": "4294430720",
     "table_name": "system.info"}
]

LINUX_INVENTORY_QUERY_SNAPSHOT = [
    {'major': '10',
     'minor': '0',
     'name': 'Debian GNU/Linux',
     'patch': '0',
     'table_name': 'os_version'},
    {'computer_name': 'debian',
     'cpu_brand': 'Intel(R) Core(TM) i7-4578U CPU @ 3.00GHz',
     'cpu_logical_cores': '1',
     'cpu_physical_cores': '1',
     'cpu_subtype': '69',
     'cpu_type': 'x86_64',
     'hardware_model': 'VMware7,1',
     'hardware_serial': 'VMware-56 4d fa 06 fd 4b 1e 89-09 ea 5d d1 32 f8 8e 12',
     'hostname': 'debian.example.com',
     'physical_memory': '1010221056',
     'table_name': 'system_info'}
]

OSX_APP_INSTANCE = {
    "bundle_id": "com.agilebits.onepassword4-updater",
    "bundle_name": "1Password Updater",
    "bundle_path": "/Applications/1Password 6.app/Contents/Helpers/1Password Updater.app",
    "bundle_version": "652003",
    "bundle_version_str": "6.5.2",
    "table_name": "apps"
}

WIN_PROGRAM_INSTANCE = {
    "identifying_number": "{0340040B-67DE-4526-B0F9-C6DF967E9822}",
    "install_date": "20210211",
    "install_location": "C:\\Program Files\\osquery\\",
    "install_source": ("C:\\Users\\User\\AppData\\Local\\Packages"
                       "\\Microsoft.MicrosoftEdge_8wekyb3d8bbwe\\TempState\\Downloads\\"),
    "language": "1033",
    "name": "osquery",
    "publisher": "osquery",
    "uninstall_string": "MsiExec.exe /X{0340040B-67DE-4526-B0F9-C6DF967E9822}",
    "version": "4.6.0",
    "table_name": "programs"
}

DEB_PACKAGE = {
    'arch': 'amd64',
    'maintainer': 'Jonathan Nieder <jrnieder@gmail.com>',
    'name': 'xz-utils',
    'priority': 'standard',
    'revision': '1',
    'section': 'utils',
    'size': '442',
    'status': 'install ok installed',
    'version': '5.2.4-1',
    "table_name": "deb_packages"
}

AZURE_AD_INFO_TUPLES = [
    {"common_name": "d14a06da-2547-4c80-9c5a-4851d1e4c7b2",
     "not_valid_before": "1556232938",
     "table_name": "azure_ad_certificate"},
    {"username": "jean",
     "key": "aadUniqueId",
     "value": "fc0e524e-9b87-4f63-a318-02727dc7983e",
     "table_name": "azure_ad_user_info"},
    {"username": "jean",
     "key": "aadUserId",
     "value": "jean@example.com",
     "table_name": "azure_ad_user_info"},
    {"username": "jean",
     "key": "version",
     "value": "1.1",
     "table_name": "azure_ad_user_info"},
    {"username": "jean",
     "key": "aadAuthorityUrl",
     "value": "https://login.microsoftonline.com/common",
     "table_name": "azure_ad_user_info"},
]


@override_settings(STATICFILES_STORAGE='django.contrib.staticfiles.storage.StaticFilesStorage')
class OsqueryAPIViewsTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.configuration = Configuration.objects.create(name=get_random_string(256))
        cls.meta_business_unit = MetaBusinessUnit.objects.create(name=get_random_string(64))
        enrollment_secret = EnrollmentSecret.objects.create(meta_business_unit=cls.meta_business_unit)
        cls.enrollment = Enrollment.objects.create(configuration=cls.configuration,
                                                   secret=enrollment_secret)
        enrollment_secret2 = EnrollmentSecret.objects.create(meta_business_unit=cls.meta_business_unit)
        cls.enrollment2 = Enrollment.objects.create(configuration=cls.configuration,
                                                    secret=enrollment_secret2)

    # utiliy methods

    def post_as_json(self, url_name, data):
        return self.client.post(reverse("osquery:{}".format(url_name)),
                                json.dumps(data),
                                content_type="application/json")

    def force_enrolled_machine(self, osquery_version="1.2.3", platform_mask=21):
        return EnrolledMachine.objects.create(
            enrollment=self.enrollment,
            serial_number=get_random_string(),
            node_key=get_random_string(),
            osquery_version=osquery_version,
            platform_mask=platform_mask
        )

    def force_query(self, force_pack=False, force_compliance_check=False, force_distributed_query=False):
        if force_compliance_check:
            sql = "select 'OK' as ztl_status;"
        else:
            sql = "select 1 from processes;"
        query = Query.objects.create(name=get_random_string(), sql=sql)
        pack = None
        if force_pack:
            pack_name = get_random_string()
            pack = Pack.objects.create(name=pack_name, slug=slugify(pack_name))
            PackQuery.objects.create(pack=pack, query=query, interval=12983,
                                     slug=slugify(query.name),
                                     log_removed_actions=False, snapshot_mode=force_compliance_check)
        sync_query_compliance_check(query, force_compliance_check)
        distributed_query = None
        if force_distributed_query:
            distributed_query = DistributedQuery.objects.create(
                query=query,
                query_version=query.version,
                sql=query.sql,
                valid_from=datetime.utcnow()
            )
        return query, pack, distributed_query

    def post_default_inventory_query_snapshot(self, node_key, platform, with_app=False, with_azure_ad=False):
        if platform == "macos":
            qs = INVENTORY_QUERY_SNAPSHOT
        elif platform == "windows":
            qs = WIN_INVENTORY_QUERY_SNAPSHOT
        else:
            qs = LINUX_INVENTORY_QUERY_SNAPSHOT
        snapshot = list(qs)
        if with_app:
            snapshot.append(OSX_APP_INSTANCE)
            snapshot.append(WIN_PROGRAM_INSTANCE)
            snapshot.append(DEB_PACKAGE)
        if with_azure_ad:
            snapshot.extend(AZURE_AD_INFO_TUPLES)
        return self.post_as_json(
            "log",
            {"node_key": node_key,
             "log_type": "result",
             "data": [{
                 'action': 'snapshot',
                 "name": INVENTORY_QUERY_NAME,
                 "snapshot": snapshot,
                 'unixTime': '1480605737',
             }]}
        )

    # enrollment

    def test_enroll_405(self):
        response = self.client.get(reverse("osquery:enroll"))
        self.assertEqual(response.status_code, 405)
        self.assertCountEqual(["POST", "OPTIONS"], (m.strip() for m in response["Allow"].split(",")))

    def test_enroll_bad_json(self):
        response = self.client.post(reverse("osquery:enroll"))
        self.assertEqual(response.status_code, 400)
        response = self.client.post(reverse("osquery:enroll"),
                                    data="lkjadslkjdsalkdjas",
                                    content_type="application/json")
        self.assertEqual(response.status_code, 400)

    def test_enroll_missing_json_keys(self):
        response = self.post_as_json("enroll", {"no_enroll_secret_key": True})
        self.assertEqual(response.status_code, 400)

    def test_enroll_bad_secret(self):
        response = self.post_as_json(
            "enroll",
            {"enroll_secret": "INVALID ENROLL SECRET",
             "host_details": {"system_info": {"hardware_serial": get_random_string(32)}}}
        )
        self.assertContains(response, "Wrong enrollment secret", status_code=403)

    def test_enroll_no_serial_number(self):
        response = self.post_as_json("enroll", {"enroll_secret": self.enrollment.secret.secret})
        self.assertEqual(response.status_code, 400)

    def test_enroll_ok(self):
        serial_number = get_random_string()
        response = self.post_as_json(
            "enroll",
            {"enroll_secret": self.enrollment.secret.secret,
             "platform_type": "21",
             "host_details": {"system_info": {"hardware_serial": serial_number},
                              "osquery_info": {"version": "1.2.3"}}}
        )
        self.assertEqual(response.status_code, 200)
        em = EnrolledMachine.objects.get(enrollment=self.enrollment, serial_number=serial_number)
        self.assertEqual(response.json(), {"node_key": em.node_key})
        self.assertEqual(em.platform_mask, 21)
        self.assertEqual(em.osquery_version, "1.2.3")
        ms = MachineSnapshot.objects.filter(source__module="zentral.contrib.osquery",
                                            serial_number=serial_number)
        self.assertEqual(ms.first().reference, em.node_key)

    def test_enroll_with_host_identifier_ok(self):
        serial_number = get_random_string()
        response = self.post_as_json(
            "enroll",
            {"enroll_secret": self.enrollment.secret.secret,
             "host_identifier": serial_number}
        )
        self.assertEqual(response.status_code, 200)
        em = EnrolledMachine.objects.get(enrollment=self.enrollment, serial_number=serial_number)
        self.assertEqual(response.json(), {"node_key": em.node_key})
        ms = MachineSnapshot.objects.filter(source__module="zentral.contrib.osquery",
                                            serial_number=serial_number)
        self.assertEqual(ms.first().reference, em.node_key)

    def test_re_enroll_same_enrollment(self):
        old_em = self.force_enrolled_machine()
        response = self.post_as_json(
            "enroll",
            {"enroll_secret": self.enrollment.secret.secret,
             "host_details": {"system_info": {"hardware_serial": old_em.serial_number}}}
        )
        self.assertEqual(response.status_code, 200)
        em = EnrolledMachine.objects.get(enrollment=self.enrollment, serial_number=old_em.serial_number)
        self.assertEqual(response.json(), {"node_key": em.node_key})
        self.assertEqual(old_em, em)

    def test_re_enroll_different_enrollment(self):
        old_em = self.force_enrolled_machine()
        response = self.post_as_json(
            "enroll",
            {"enroll_secret": self.enrollment2.secret.secret,
             "host_details": {"system_info": {"hardware_serial": old_em.serial_number}}}
        )
        self.assertEqual(response.status_code, 200)
        em = EnrolledMachine.objects.get(enrollment=self.enrollment2, serial_number=old_em.serial_number)
        self.assertEqual(response.json(), {"node_key": em.node_key})
        self.assertEqual(EnrolledMachine.objects.filter(serial_number=old_em.serial_number).count(), 1)

    # config

    def test_config_405(self):
        response = self.client.get(reverse("osquery:enroll"))
        self.assertEqual(response.status_code, 405)
        self.assertCountEqual(["POST", "OPTIONS"], (m.strip() for m in response["Allow"].split(",")))

    def test_config_missing_node_key(self):
        response = self.post_as_json("config", {"godzilla": "ffm"})
        self.assertEqual(response.status_code, 400)

    def test_config_wrong_node_key(self):
        response = self.post_as_json("config", {"node_key": "godzilla"})
        self.assertContains(response, "Wrong node_key", status_code=403)

    def test_config_ok(self):
        em = self.force_enrolled_machine()
        response = self.post_as_json("config", {"node_key": em.node_key})
        self.assertEqual(response.status_code, 200)
        json_response = response.json()
        self.assertIn("schedule", json_response)
        schedule = json_response["schedule"]
        self.assertIn(INVENTORY_QUERY_NAME, schedule)

    def test_osx_app_instance_schedule(self):
        em = self.force_enrolled_machine()
        self.post_default_inventory_query_snapshot(em.node_key, platform="macos")
        response = self.post_as_json("config", {"node_key": em.node_key})
        self.assertEqual(response.status_code, 200)
        json_response = response.json()
        self.assertIn("schedule", json_response)
        schedule = json_response["schedule"]
        self.assertIn(INVENTORY_QUERY_NAME, schedule)
        self.assertNotIn(" 'apps' ", schedule[INVENTORY_QUERY_NAME]["query"])
        self.configuration.inventory_apps = True
        self.configuration.save()
        response = self.post_as_json("config", {"node_key": em.node_key})
        self.assertEqual(response.status_code, 200)
        json_response = response.json()
        self.assertIn("schedule", json_response)
        schedule = json_response["schedule"]
        self.assertIn(INVENTORY_QUERY_NAME, schedule)
        self.assertIn(" 'apps' ", schedule[INVENTORY_QUERY_NAME]["query"])

    def test_win_program_instance_schedule(self):
        em = self.force_enrolled_machine()
        self.post_default_inventory_query_snapshot(em.node_key, platform="windows")
        response = self.post_as_json("config", {"node_key": em.node_key})
        self.assertEqual(response.status_code, 200)
        json_response = response.json()
        self.assertIn("schedule", json_response)
        schedule = json_response["schedule"]
        self.assertIn(INVENTORY_QUERY_NAME, schedule)
        self.assertNotIn(" 'apps' ", schedule[INVENTORY_QUERY_NAME]["query"])
        self.configuration.inventory_apps = True
        self.configuration.save()
        response = self.post_as_json("config", {"node_key": em.node_key})
        self.assertEqual(response.status_code, 200)
        json_response = response.json()
        self.assertIn("schedule", json_response)
        schedule = json_response["schedule"]
        self.assertIn(INVENTORY_QUERY_NAME, schedule)
        self.assertIn(" 'programs' ", schedule[INVENTORY_QUERY_NAME]["query"])

    def test_deb_packages_schedule(self):
        em = self.force_enrolled_machine()
        self.post_default_inventory_query_snapshot(em.node_key, platform="linux")
        response = self.post_as_json("config", {"node_key": em.node_key})
        self.assertEqual(response.status_code, 200)
        json_response = response.json()
        self.assertIn("schedule", json_response)
        schedule = json_response["schedule"]
        self.assertIn(INVENTORY_QUERY_NAME, schedule)
        self.assertNotIn(" 'apps' ", schedule[INVENTORY_QUERY_NAME]["query"])
        self.configuration.inventory_apps = True
        self.configuration.save()
        response = self.post_as_json("config", {"node_key": em.node_key})
        self.assertEqual(response.status_code, 200)
        json_response = response.json()
        self.assertIn("schedule", json_response)
        schedule = json_response["schedule"]
        self.assertIn(INVENTORY_QUERY_NAME, schedule)
        self.assertIn(" 'deb_packages' ", schedule[INVENTORY_QUERY_NAME]["query"])

    # distributed queries

    def test_distributed_read_405(self):
        response = self.client.get(reverse("osquery:distributed_read"))
        self.assertEqual(response.status_code, 405)
        self.assertCountEqual(["POST", "OPTIONS"], (m.strip() for m in response["Allow"].split(",")))

    def test_distributed_read_missing_node_key(self):
        response = self.post_as_json("distributed_read", {"godzilla": "ffm"})
        self.assertEqual(response.status_code, 400)

    def test_distributed_read_wrong_node_key(self):
        response = self.post_as_json("distributed_read", {"node_key": "godzilla"})
        self.assertContains(response, "Wrong node_key", status_code=403)

    def test_distributed_read_empty(self):
        em = self.force_enrolled_machine()
        response = self.post_as_json("distributed_read", {"node_key": em.node_key})
        self.assertEqual(response.status_code, 200)
        json_response = response.json()
        self.assertEqual(json_response, {"queries": {}})

    def test_distributed_read_one_query(self):
        em = self.force_enrolled_machine(osquery_version="17.0.0", platform_mask=21)
        dq = DistributedQuery.objects.create(sql="select username from users;",
                                             # no minimum osquery version
                                             # no platforms
                                             valid_from=datetime.utcnow(),
                                             query_version=1)
        dq2 = DistributedQuery.objects.create(sql="select * from osquery_schedule;",
                                              minimum_osquery_version="17.0.0",  # OK
                                              platforms=["darwin"],  # OK
                                              valid_from=datetime.utcnow(),
                                              query_version=1)
        DistributedQuery.objects.create(sql="select username from users;",
                                        minimum_osquery_version="18.0.0",  # too high
                                        platforms=["darwin"],  # OK
                                        valid_from=datetime.utcnow(),
                                        query_version=1)
        DistributedQuery.objects.create(sql="select username from users;",
                                        minimum_osquery_version="17.0.0",  # OK
                                        platforms=["linux"],  # wrong platform
                                        valid_from=datetime.utcnow(),
                                        query_version=1)
        response = self.post_as_json("distributed_read", {"node_key": em.node_key})
        self.assertEqual(response.status_code, 200)
        dqm_qs = (DistributedQueryMachine.objects.filter(serial_number=em.serial_number)
                                                 .order_by("distributed_query__pk"))
        self.assertEqual(dqm_qs.count(), 2)
        dqm, dqm2 = tuple(dqm_qs)
        self.assertEqual(dqm.distributed_query, dq)
        self.assertEqual(dqm.status, None)
        self.assertEqual(dqm2.distributed_query, dq2)
        self.assertEqual(dqm2.status, None)
        json_response = response.json()
        self.assertEqual(json_response, {"queries": {str(dqm.pk): dq.sql,
                                                     str(dqm2.pk): dq2.sql}})
        response = self.post_as_json("distributed_read", {"node_key": em.node_key})
        self.assertEqual(response.status_code, 200)
        json_response = response.json()
        self.assertEqual(json_response, {"queries": {}})
        self.assertEqual(dqm_qs.count(), 2)

    def test_distributed_write_405(self):
        response = self.client.get(reverse("osquery:distributed_write"))
        self.assertEqual(response.status_code, 405)
        self.assertCountEqual(["POST", "OPTIONS"], (m.strip() for m in response["Allow"].split(",")))

    def test_distributed_write_missing_node_key(self):
        response = self.post_as_json("distributed_write", {"godzilla": "ffm"})
        self.assertEqual(response.status_code, 400)

    def test_distributed_write_wrong_node_key(self):
        response = self.post_as_json("distributed_write", {"node_key": "godzilla"})
        self.assertContains(response, "Wrong node_key", status_code=403)

    def test_distributed_write_no_compliance_check(self):
        em = self.force_enrolled_machine()
        dq = DistributedQuery.objects.create(sql="select username from users;",
                                             valid_from=datetime.utcnow(),
                                             query_version=1)
        dqm = DistributedQueryMachine.objects.create(distributed_query=dq, serial_number=em.serial_number)
        response = self.post_as_json("distributed_write",
                                     {"node_key": em.node_key,
                                      "queries": {str(dqm.pk): [{"username": "godzilla"}]},
                                      "statuses": {str(dqm.pk): 0}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {})
        dqm.refresh_from_db()
        self.assertEqual(dqm.status, 0)
        dqr_qs = DistributedQueryResult.objects.filter(distributed_query=dq, serial_number=em.serial_number)
        self.assertEqual(dqr_qs.count(), 1)
        self.assertEqual(dqr_qs.first().row, {"username": "godzilla"})
        ms_qs = MachineStatus.objects.filter(serial_number=em.serial_number)
        self.assertEqual(ms_qs.count(), 0)

    @patch("zentral.core.queues.backends.kombu.EventQueues.post_event")
    def test_distributed_write_two_distributed_queries_one_compliance_check(self, post_event):
        query1, _, distributed_query1 = self.force_query(force_distributed_query=True, force_compliance_check=True)
        query2, _, distributed_query2 = self.force_query(force_distributed_query=True, force_compliance_check=False)
        em = self.force_enrolled_machine()
        dqm1 = DistributedQueryMachine.objects.create(distributed_query=distributed_query1,
                                                      serial_number=em.serial_number)
        dqm2 = DistributedQueryMachine.objects.create(distributed_query=distributed_query2,
                                                      serial_number=em.serial_number)
        response = self.post_as_json("distributed_write",
                                     {"node_key": em.node_key,
                                      "queries": {str(dqm1.pk): [{"ztl_status": Status.OK.name}],
                                                  str(dqm2.pk): [{"username": "godzilla"}]},
                                      "statuses": {str(dqm1.pk): 0,
                                                   str(dqm2.pk): 0}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {})
        ms_qs = MachineStatus.objects.filter(serial_number=em.serial_number, compliance_check=query1.compliance_check)
        self.assertEqual(ms_qs.count(), 1)
        ms = ms_qs.first()
        self.assertEqual(ms.compliance_check, query1.compliance_check)
        self.assertEqual(ms.compliance_check_version, query1.version)
        self.assertEqual(ms.compliance_check_version, query1.compliance_check.version)
        self.assertEqual(ms.compliance_check_version, distributed_query1.query_version)
        self.assertEqual(ms.status, Status.OK.value)
        events = list(call_args.args[0] for call_args in post_event.call_args_list)
        self.assertEqual(len(events), 2)
        request_event = events[0]
        self.assertIsInstance(request_event, OsqueryRequestEvent)
        self.assertEqual(request_event.payload["request_type"], "distributed_write")
        cc_status_event = events[1]
        self.assertIsInstance(cc_status_event, OsqueryCheckStatusUpdated)
        self.assertEqual(cc_status_event.payload["osquery_run"], {"pk": distributed_query1.pk})
        self.assertEqual(cc_status_event.get_linked_objects_keys(),
                         {"compliance_check": [(query1.compliance_check.pk,)],
                          "osquery_run": [(distributed_query1.pk,)],
                          "osquery_query": [(query1.pk,)]})

    @patch("zentral.core.queues.backends.kombu.EventQueues.post_event")
    def test_distributed_write_two_distributed_queries_one_outdated_version_compliance_check(self, post_event):
        query1, _, distributed_query1 = self.force_query(force_distributed_query=True, force_compliance_check=True)
        query1.version = 127
        query1.save()
        query2, _, distributed_query2 = self.force_query(force_distributed_query=True, force_compliance_check=False)
        em = self.force_enrolled_machine()
        dqm1 = DistributedQueryMachine.objects.create(distributed_query=distributed_query1,
                                                      serial_number=em.serial_number)
        dqm2 = DistributedQueryMachine.objects.create(distributed_query=distributed_query2,
                                                      serial_number=em.serial_number)
        response = self.post_as_json("distributed_write",
                                     {"node_key": em.node_key,
                                      "queries": {str(dqm1.pk): [{"ztl_status": Status.OK.name}],
                                                  str(dqm2.pk): [{"username": "godzilla"}]},
                                      "statuses": {str(dqm1.pk): 0,
                                                   str(dqm2.pk): 0}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {})
        ms_qs = MachineStatus.objects.filter(serial_number=em.serial_number, compliance_check=query1.compliance_check)
        self.assertEqual(ms_qs.count(), 0)  # distributed query version < query version
        events = list(call_args.args[0] for call_args in post_event.call_args_list)
        self.assertEqual(len(events), 1)
        request_event = events[0]
        self.assertIsInstance(request_event, OsqueryRequestEvent)
        self.assertEqual(request_event.payload["request_type"], "distributed_write")

    # log

    def test_log_405(self):
        response = self.client.get(reverse("osquery:log"))
        self.assertEqual(response.status_code, 405)
        self.assertCountEqual(["POST", "OPTIONS"], (m.strip() for m in response["Allow"].split(",")))

    def test_log_missing_node_key(self):
        response = self.post_as_json("log", {"godzilla": "ffm"})
        self.assertEqual(response.status_code, 400)

    def test_log_wrong_node_key(self):
        response = self.post_as_json("log", {"node_key": "godzilla"})
        self.assertContains(response, "Wrong node_key", status_code=403)

    def test_log_default_inventory_query(self):
        em = self.force_enrolled_machine()
        response = self.post_default_inventory_query_snapshot(em.node_key, platform="macos", with_app=True)
        self.assertEqual(response.status_code, 200)
        json_response = response.json()
        self.assertEqual(json_response, {})
        ms = MachineSnapshot.objects.current().get(serial_number=em.serial_number, reference=em.node_key)
        self.assertEqual(ms.os_version.build, INVENTORY_QUERY_SNAPSHOT[0]["build"])
        self.assertEqual(ms.system_info.hardware_model, INVENTORY_QUERY_SNAPSHOT[1]["hardware_model"].strip(" \u0000"))
        self.assertEqual(list(ms.osx_app_instances.values_list("app__bundle_name", flat=True)),
                         [OSX_APP_INSTANCE["bundle_name"]])
        self.assertEqual(list(ms.program_instances.values_list("program__name", flat=True)),
                         [WIN_PROGRAM_INSTANCE["name"]])
        self.assertEqual(list(ms.deb_packages.values_list("name", flat=True)),
                         [DEB_PACKAGE["name"]])

    def test_log_status(self):
        em = self.force_enrolled_machine()
        post_data = {
            "node_key": em.node_key,
            "log_type": "status",
            "data": [
                {'filename': 'scheduler.cpp',
                 'line': '63',
                 'message': 'Executing scheduled query: macos-attacks-query-pack_604dc4d3: '
                            "select * from startup_items where path like '%iWorkServices%';",
                 'severity': '0',
                 'version': '2.1.2',
                 'unixTime': '1480605737'}
            ]
        }
        response = self.post_as_json("log", post_data)
        json_response = response.json()
        self.assertEqual(json_response, {})

    def test_log_added_result(self):
        em = self.force_enrolled_machine()
        query, pack, _ = self.force_query(force_pack=True)
        post_data = {
            "node_key": em.node_key,
            "log_type": "result",
            "data": [
                {'name': Pack.DELIMITER.join(['pack', pack.configuration_key(), query.packquery.pack_key()]),
                 'action': 'added',
                 'hostIdentifier': 'godzilla.local',
                 'columns': {'name': 'Dropbox', 'pid': '1234', 'port': '17500'},
                 'unixTime': '1480605737'}
            ]
        }
        response = self.post_as_json("log", post_data)
        json_response = response.json()
        self.assertEqual(json_response, {})

    @patch("zentral.core.queues.backends.kombu.EventQueues.post_event")
    def test_log_snapshot_result(self, post_event):
        em = self.force_enrolled_machine()
        query, pack, _ = self.force_query(force_pack=True)
        post_data = {
            "node_key": em.node_key,
            "log_type": "result",
            "data": [
                {'name': Pack.DELIMITER.join(['pack', pack.configuration_key(), query.packquery.pack_key()]),
                 'action': 'snapshot',
                 'hostIdentifier': 'godzilla.local',
                 "snapshot": [
                     {
                         "parent": "0",
                         "path": "/sbin/launchd",
                         "pid": "1"
                     },
                     {
                         "parent": "1",
                         "path": "/usr/sbin/syslogd",
                         "pid": "51"
                     }
                 ],
                 'unixTime': '1480605737'}
            ]
        }
        response = self.post_as_json("log", post_data)
        json_response = response.json()
        self.assertEqual(json_response, {})
        events = list(call_args.args[0] for call_args in post_event.call_args_list)
        self.assertEqual(len(events), 2)
        request_event = events[0]
        self.assertIsInstance(request_event, OsqueryRequestEvent)
        self.assertEqual(request_event.payload["request_type"], "log")
        result_event = events[1]
        self.assertIsInstance(result_event, OsqueryResultEvent)
        self.assertEqual(result_event.get_linked_objects_keys(),
                         {"osquery_pack": [(pack.pk,)],
                          "osquery_query": [(query.pk,)]})

    @patch("zentral.core.queues.backends.kombu.EventQueues.post_event")
    def test_log_snapshot_result_with_compliance_check(self, post_event):
        em = self.force_enrolled_machine()
        query1, pack1, _ = self.force_query(force_pack=True, force_compliance_check=True)
        status_time0 = datetime(2021, 12, 23)
        status_time1 = datetime(2021, 12, 24)
        query2, pack2, _ = self.force_query(force_pack=True, force_compliance_check=True)
        status_time2 = datetime(2021, 12, 25)
        post_data = {
            "node_key": em.node_key,
            "log_type": "result",
            "data": [
                {'name': Pack.DELIMITER.join(['pack', pack1.configuration_key(), query1.packquery.pack_key()]),
                 'action': 'snapshot',
                 'hostIdentifier': 'godzilla.local',
                 "snapshot": [{"ztl_status": Status.OK.name}],
                 "unixTime": status_time0.strftime('%s')},
                {'name': Pack.DELIMITER.join(['pack', pack1.configuration_key(), query1.packquery.pack_key()]),
                 'action': 'snapshot',
                 'hostIdentifier': 'godzilla.local',
                 "snapshot": [{"ztl_status": Status.FAILED.name}],
                 "unixTime": status_time1.strftime('%s')},
                {'name': Pack.DELIMITER.join(['pack', pack2.configuration_key(), query2.packquery.pack_key()]),
                 'action': 'snapshot',
                 'hostIdentifier': 'godzilla.local',
                 "snapshot": [],
                 "unixTime": status_time2.strftime('%s')}
            ]
        }
        response = self.post_as_json("log", post_data)
        json_response = response.json()
        self.assertEqual(json_response, {})
        ms1_qs = MachineStatus.objects.filter(serial_number=em.serial_number, compliance_check=query1.compliance_check)
        self.assertEqual(ms1_qs.count(), 1)
        ms1 = ms1_qs.first()
        self.assertEqual(ms1.compliance_check, query1.compliance_check)
        self.assertEqual(ms1.compliance_check_version, query1.version)
        self.assertEqual(ms1.compliance_check_version, query1.compliance_check.version)
        self.assertEqual(ms1.status_time, status_time1)
        self.assertEqual(ms1.status, Status.FAILED.value)
        ms2_qs = MachineStatus.objects.filter(serial_number=em.serial_number, compliance_check=query2.compliance_check)
        self.assertEqual(ms2_qs.count(), 1)
        ms2 = ms2_qs.first()
        self.assertEqual(ms2.compliance_check, query2.compliance_check)
        self.assertEqual(ms2.compliance_check_version, query2.version)
        self.assertEqual(ms2.compliance_check_version, query2.compliance_check.version)
        self.assertEqual(ms2.status_time, status_time2)
        self.assertEqual(ms2.status, Status.UNKNOWN.value)
        events = list(call_args.args[0] for call_args in post_event.call_args_list)
        self.assertEqual(len(events), 6)
        request_event = events[0]
        self.assertIsInstance(request_event, OsqueryRequestEvent)
        self.assertEqual(request_event.payload["request_type"], "log")
        for result_event in events[1:4]:
            self.assertIsInstance(result_event, OsqueryResultEvent)
        for cc_status_event in events[4:]:
            self.assertIsInstance(cc_status_event, OsqueryCheckStatusUpdated)
            if cc_status_event.payload["status"] == Status.UNKNOWN.name:
                self.assertEqual(cc_status_event.payload["osquery_query"], {"pk": query2.pk})
                self.assertEqual(cc_status_event.metadata.created_at, status_time2)
                self.assertEqual(cc_status_event.get_linked_objects_keys(),
                                 {"compliance_check": [(query2.compliance_check.pk,)],
                                  "osquery_pack": [(pack2.pk,)],
                                  "osquery_query": [(query2.pk,)]})
            else:
                self.assertEqual(cc_status_event.payload["osquery_query"], {"pk": query1.pk})
                self.assertEqual(cc_status_event.payload["status"], Status.FAILED.name)
                self.assertEqual(cc_status_event.metadata.created_at, status_time1)
                self.assertEqual(cc_status_event.get_linked_objects_keys(),
                                 {"compliance_check": [(query1.compliance_check.pk,)],
                                  "osquery_pack": [(pack1.pk,)],
                                  "osquery_query": [(query1.pk,)]})
