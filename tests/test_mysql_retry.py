from flask_app.mysql_retry import statement_allows_connection_retry


def test_plain_reads_may_be_retried_after_connection_loss():
    assert statement_allows_connection_retry("SELECT value FROM app_settings WHERE `key` = %s")
    assert statement_allows_connection_retry("SHOW TABLES")
    assert statement_allows_connection_retry("DESCRIBE tank_data")
    assert statement_allows_connection_retry("EXPLAIN SELECT * FROM tank_data")


def test_writes_and_locking_reads_are_never_replayed_automatically():
    assert not statement_allows_connection_retry("INSERT INTO tank_data(device_id) VALUES (%s)")
    assert not statement_allows_connection_retry("UPDATE app_settings SET value = %s")
    assert not statement_allows_connection_retry("DELETE FROM device_events WHERE id = %s")
    assert not statement_allows_connection_retry("CREATE TABLE example(id INT)")
    assert not statement_allows_connection_retry("SELECT * FROM device_command_queue FOR UPDATE")
    assert not statement_allows_connection_retry(
        "SELECT * FROM device_command_queue LOCK IN SHARE MODE"
    )
