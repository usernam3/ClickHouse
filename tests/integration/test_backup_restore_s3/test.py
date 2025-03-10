from typing import Dict, Iterable
import pytest
from helpers.cluster import ClickHouseCluster
from helpers.test_tools import TSV


cluster = ClickHouseCluster(__file__)
node = cluster.add_instance(
    "node",
    main_configs=[
        "configs/disk_s3.xml",
        "configs/named_collection_s3_backups.xml",
        "configs/s3_settings.xml",
    ],
    user_configs=[
        "configs/zookeeper_retries.xml",
    ],
    with_minio=True,
)


@pytest.fixture(scope="module", autouse=True)
def start_cluster():
    try:
        cluster.start()
        yield
    finally:
        cluster.shutdown()


backup_id_counter = 0


def new_backup_name():
    global backup_id_counter
    backup_id_counter += 1
    return f"backup{backup_id_counter}"


def get_events(events_names: Iterable[str]) -> Dict[str, int]:
    _events = TSV(
        node.query(
            f"SELECT event, value FROM system.events WHERE event in {events_names} SETTINGS system_events_show_zero_values = 1;"
        )
    )
    return {
        event: int(value)
        for event, value in [line.split("\t") for line in _events.lines]
    }


def check_backup_and_restore(
    storage_policy, backup_destination, size=1000, backup_name=None, check_events=False
):
    s3_backup_events = (
        "WriteBufferFromS3Microseconds",
        "WriteBufferFromS3Bytes",
        "WriteBufferFromS3RequestsErrors",
    )
    s3_restore_events = (
        "ReadBufferFromS3Microseconds",
        "ReadBufferFromS3Bytes",
        "ReadBufferFromS3RequestsErrors",
    )

    node.query(
        f"""
    DROP TABLE IF EXISTS data SYNC;
    CREATE TABLE data (key Int, value String, array Array(String)) Engine=MergeTree() ORDER BY tuple() SETTINGS storage_policy='{storage_policy}';
    INSERT INTO data SELECT * FROM generateRandom('key Int, value String, array Array(String)') LIMIT {size};
    OPTIMIZE TABLE data FINAL;
    """
    )
    try:
        events_before_backups = get_events(s3_backup_events)
        node.query(f"BACKUP TABLE data TO {backup_destination}")
        events_after_backups = get_events(s3_backup_events)
        events_before_restore = get_events(s3_restore_events)
        node.query(
            f"""
            RESTORE TABLE data AS data_restored FROM {backup_destination};
            """
        )
        events_after_restore = get_events(s3_restore_events)
        node.query(
            """
            SELECT throwIf(
                (SELECT count(), sum(sipHash64(*)) FROM data) !=
                (SELECT count(), sum(sipHash64(*)) FROM data_restored),
                'Data does not matched after BACKUP/RESTORE'
        );
        """
        )
        if check_events and backup_name:
            objects = node.cluster.minio_client.list_objects(
                "root", f"data/backups/multipart/{backup_name}/"
            )
            backup_meta_size = 0
            for obj in objects:
                if ".backup" in obj.object_name:
                    backup_meta_size = obj.size
                    break
            backup_total_size = int(
                node.query(
                    f"SELECT sum(total_size) FROM system.backups WHERE status = 'BACKUP_CREATED' AND name like '%{backup_name}%'"
                ).strip()
            )
            restore_total_size = int(
                node.query(
                    f"SELECT sum(total_size) FROM system.backups WHERE status = 'RESTORED' AND name like '%{backup_name}%'"
                ).strip()
            )
            # backup
            # NOTE: ~35 bytes is used by .lock file, so set up 100 bytes to avoid flaky test
            assert (
                abs(
                    backup_total_size
                    - (
                        events_after_backups["WriteBufferFromS3Bytes"]
                        - events_before_backups["WriteBufferFromS3Bytes"]
                        - backup_meta_size
                    )
                )
                < 100
            )
            assert (
                events_after_backups["WriteBufferFromS3Microseconds"]
                > events_before_backups["WriteBufferFromS3Microseconds"]
            )
            assert events_after_backups["WriteBufferFromS3RequestsErrors"] == 0
            # restore
            assert (
                events_after_restore["ReadBufferFromS3Bytes"]
                - events_before_restore["ReadBufferFromS3Bytes"]
                - backup_meta_size
                == restore_total_size
            )
            assert (
                events_after_restore["ReadBufferFromS3Microseconds"]
                > events_before_restore["ReadBufferFromS3Microseconds"]
            )
            assert events_after_restore["ReadBufferFromS3RequestsErrors"] == 0
    finally:
        node.query(
            """
            DROP TABLE data SYNC;
            DROP TABLE IF EXISTS data_restored SYNC;
            """
        )


def check_system_tables():
    disks = [
        tuple(disk.split("\t"))
        for disk in node.query("SELECT name, type FROM system.disks").split("\n")
        if disk
    ]
    expected_disks = (
        ("default", "local"),
        ("disk_s3", "s3"),
        ("disk_s3_other_bucket", "s3"),
        ("disk_s3_plain", "s3_plain"),
    )
    assert len(expected_disks) == len(disks)
    for expected_disk in expected_disks:
        if expected_disk not in disks:
            raise AssertionError(f"Missed {expected_disk} in {disks}")


@pytest.mark.parametrize(
    "storage_policy, to_disk",
    [
        pytest.param(
            "default",
            "default",
            id="from_local_to_local",
        ),
        pytest.param(
            "policy_s3",
            "default",
            id="from_s3_to_local",
        ),
        pytest.param(
            "default",
            "disk_s3",
            id="from_local_to_s3",
        ),
        pytest.param(
            "policy_s3",
            "disk_s3_plain",
            id="from_s3_to_s3_plain",
        ),
        pytest.param(
            "default",
            "disk_s3_plain",
            id="from_local_to_s3_plain",
        ),
    ],
)
def test_backup_to_disk(storage_policy, to_disk):
    backup_name = new_backup_name()
    backup_destination = f"Disk('{to_disk}', '{backup_name}')"
    check_backup_and_restore(storage_policy, backup_destination)


def test_backup_to_s3():
    storage_policy = "default"
    backup_name = new_backup_name()
    backup_destination = (
        f"S3('http://minio1:9001/root/data/backups/{backup_name}', 'minio', 'minio123')"
    )
    check_backup_and_restore(storage_policy, backup_destination)
    check_system_tables()


def test_backup_to_s3_named_collection():
    storage_policy = "default"
    backup_name = new_backup_name()
    backup_destination = f"S3(named_collection_s3_backups, '{backup_name}')"
    check_backup_and_restore(storage_policy, backup_destination)


def test_backup_to_s3_multipart():
    storage_policy = "default"
    backup_name = new_backup_name()
    backup_destination = f"S3('http://minio1:9001/root/data/backups/multipart/{backup_name}', 'minio', 'minio123')"
    check_backup_and_restore(
        storage_policy,
        backup_destination,
        size=1000000,
        backup_name=backup_name,
        check_events=True,
    )
    assert node.contains_in_log(
        f"copyDataToS3File: Multipart upload has completed. Bucket: root, Key: data/backups/multipart/{backup_name}"
    )


def test_backup_to_s3_native_copy():
    storage_policy = "policy_s3"
    backup_name = new_backup_name()
    backup_destination = (
        f"S3('http://minio1:9001/root/data/backups/{backup_name}', 'minio', 'minio123')"
    )
    check_backup_and_restore(storage_policy, backup_destination)
    assert node.contains_in_log("BackupWriterS3.*using native copy")
    assert node.contains_in_log("BackupReaderS3.*using native copy")
    assert node.contains_in_log(
        f"copyS3File: Single operation copy has completed. Bucket: root, Key: data/backups/{backup_name}"
    )


def test_backup_to_s3_native_copy_other_bucket():
    storage_policy = "policy_s3_other_bucket"
    backup_name = new_backup_name()
    backup_destination = (
        f"S3('http://minio1:9001/root/data/backups/{backup_name}', 'minio', 'minio123')"
    )
    check_backup_and_restore(storage_policy, backup_destination)
    assert node.contains_in_log("BackupWriterS3.*using native copy")
    assert node.contains_in_log("BackupReaderS3.*using native copy")
    assert node.contains_in_log(
        f"copyS3File: Single operation copy has completed. Bucket: root, Key: data/backups/{backup_name}"
    )


def test_backup_to_s3_native_copy_multipart():
    storage_policy = "policy_s3"
    backup_name = new_backup_name()
    backup_destination = f"S3('http://minio1:9001/root/data/backups/multipart/{backup_name}', 'minio', 'minio123')"
    check_backup_and_restore(storage_policy, backup_destination, size=1000000)
    assert node.contains_in_log("BackupWriterS3.*using native copy")
    assert node.contains_in_log("BackupReaderS3.*using native copy")
    assert node.contains_in_log(
        f"copyS3File: Multipart upload has completed. Bucket: root, Key: data/backups/multipart/{backup_name}/"
    )


def test_incremental_backup_append_table_def():
    backup_name = f"S3('http://minio1:9001/root/data/backups/{new_backup_name()}', 'minio', 'minio123')"

    node.query(
        "CREATE TABLE data (x UInt32, y String) Engine=MergeTree() ORDER BY y PARTITION BY x%10 SETTINGS storage_policy='policy_s3'"
    )

    node.query("INSERT INTO data SELECT number, toString(number) FROM numbers(100)")
    assert node.query("SELECT count(), sum(x) FROM data") == "100\t4950\n"

    node.query(f"BACKUP TABLE data TO {backup_name}")

    node.query("ALTER TABLE data MODIFY SETTING parts_to_throw_insert=100")

    incremental_backup_name = f"S3('http://minio1:9001/root/data/backups/{new_backup_name()}', 'minio', 'minio123')"

    node.query(
        f"BACKUP TABLE data TO {incremental_backup_name} SETTINGS base_backup = {backup_name}"
    )

    node.query("DROP TABLE data")
    node.query(f"RESTORE TABLE data FROM {incremental_backup_name}")

    assert node.query("SELECT count(), sum(x) FROM data") == "100\t4950\n"
    assert "parts_to_throw_insert = 100" in node.query("SHOW CREATE TABLE data")
