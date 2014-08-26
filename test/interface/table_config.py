#!/usr/bin/env python
# Copyright 2010-2014 RethinkDB, all rights reserved.
import sys, os, time, traceback
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), os.path.pardir, 'common')))
import driver, scenario_common, utils
from vcoptparse import *
r = utils.import_python_driver()

"""The `interface.table_config` test checks that the special `rethinkdb.table_config` and
`rethinkdb.table_status` tables behave as expected."""

op = OptParser()
scenario_common.prepare_option_parser_mode_flags(op)
opts = op.parse(sys.argv)

with driver.Metacluster() as metacluster:
    cluster1 = driver.Cluster(metacluster)
    executable_path, command_prefix, serve_options = scenario_common.parse_mode_flags(opts)
    print "Spinning up two processes..."
    files1 = driver.Files(metacluster, log_path = "create-output-1", machine_name = "a",
                          executable_path = executable_path, command_prefix = command_prefix)
    proc1 = driver.Process(cluster1, files1, log_path = "serve-output-1",
        executable_path = executable_path, command_prefix = command_prefix, extra_options = serve_options)
    files2 = driver.Files(metacluster, log_path = "create-output-2", machine_name = "b",
                          executable_path = executable_path, command_prefix = command_prefix)
    proc2 = driver.Process(cluster1, files2, log_path = "serve-output-2",
        executable_path = executable_path, command_prefix = command_prefix, extra_options = serve_options)
    proc1.wait_until_started_up()
    proc2.wait_until_started_up()
    cluster1.check()
    conn = r.connect("localhost", proc1.driver_port)

    def check_foo_config_matches(expected):
        config = r.db("rethinkdb").table("table_config").filter({"name": "foo"}) \
                  .run(conn)
        assert len(config) == 1
        assert config[0]["name"] == "foo"
        found = config[0]["shards"]
        if len(expected) != len(found):
            return False
        for (e_shard, f_shard) in zip(expected, found):
            if set(e_shard["replicas"]) != set(f_shard["replicas"]):
                return False
            if e_shard["directors"] != f_shard["directors"]:
                return False
        return True

    def check_status_matches_config():
        config = r.db("rethinkdb").table("table_config").run(conn)
        status = r.db("rethinkdb").table("table_status").run(conn)
        uuids = set(row["uuid"] for row in config)
        if not (len(uuids) == len(config) == len(status)):
            return False
        if uuids != set(row["uuid"] for row in status):
            return False
        for c_row in config:
            s_row = [row for row in status if row["uuid"] == c_row["uuid"]][0]
            if c_row["db"] != s_row["db"]:
                return False
            if c_row["name"] != s_row["name"]:
                return False
            c_shards = c_row["shards"]
            s_shards = s_row["shards"]
            if len(s_shards) != len(c_shards):
                return False
            for (s_shard, c_shard) in zip(s_shards, c_shards):
                if set(s_shard.keys()) != set(c_shard["replicas"]):
                    return False
                s_directors = [server for (server, server_status) in s_shard.iteritems()
                               if server_status["role"] == "director"]
                if len(s_directors) != 1:
                    return False
                if s_directors[0] not in c_shard["directors"]:
                    return False
                if any(server_status["state"] != "ready"
                       for server_status in s_shard.values()):
                    return False
        return True

    def check_tables_named(names):
        config = r.db("rethinkdb").table("table_config").run(conn)
        if not all(row["db"] == "test" for row in config):
            return False
        if set(row["name"] for row in config) != set(names):
            return False
        return True

    def wait_until(condition):
        try:
            start_time = time.time()
            while not condition():
                time.sleep(1)
                if time.time() > start_time + 10:
                    raise RuntimeError("Out of time")
        except:
            config = r.db("rethinkdb").table("table_config").run(conn)
            status = r.db("rethinkdb").table("table_status").run(conn)
            print "Something went wrong."
            print "config =", config
            print "status =", status
            raise

    print "Creating a table..."
    r.db_create("test").run(conn)
    r.table_create("foo").run(conn)
    r.table_create("bar").run(conn)
    r.table("foo").insert([{"i": i} for i in xrange(10)]).run(conn)
    assert set(row["i"] for row in r.table("foo").run(conn)) == set(xrange(10))

    print "Testing that table_config and table_status are sane..."
    wait_until(lambda: check_tables_named(["foo", "bar"]))
    wait_until(check_status_matches_config)

    print "Testing that we can write to table_config..."
    def test(shards):
        print "Reconfiguring:", shards
        # RSI(reql_admin): Use porcelain here
        res = r.db("rethinkdb").table("table_config").filter({"name": "foo"}) \
               .update({"shards": shards}).run(conn)
        assert res["errors"] == 0
        wait_until(lambda: check_foo_config_matches(shards))
        wait_until(check_status_matches_config)
        assert set(row["i"] for row in r.table("foo").run(conn)) == set(xrange(10))
        print "OK"
    test(
        [{"replicas": ["a"], "directors": ["a"]}])
    test(
        [{"replicas": ["b"], "directors": ["b"]}])
    test(
        [{"replicas": ["a", "b"], "directors": ["a"]}])
    test(
        [{"replicas": ["a"], "directors": ["a"]},
         {"replicas": ["b"], "directors": ["b"]}])
    test(
        [{"replicas": ["a", "b"], "directors": ["a"]},
         {"replicas": ["a", "b"], "directors": ["b"]}])
    test(
        [{"replicas": ["a"], "directors": ["a"]}])

    print "Testing that table_config rejects invalid input..."
    def test_invalid(shards):
        print "Reconfiguring:", shards
        res = r.db("rethinkdb").table("table_config").filter({"name": "foo"}).update(
                {"shards": shards}).run(conn)
        assert res["errors"] == 1
        print "Error, as expected"
    test_invalid([])
    test_invalid("this is a string")
    test_invalid(
        [{"replicas": ["a"], "directors": ["b"], "extra_key": "extra_value"}])
    test_invalid(
        [{"replicas": [], "directors": []}])
    test_invalid(
        [{"replicas": ["a"], "directors": []}])
    test_invalid(
        [{"replicas": ["a"], "directors": ["b"]},
         {"replicas": ["b"], "directors": ["a"]}])

    print "Testing that we can rename tables through table_config..."
    res = r.db("rethinkdb").table("table_config").filter({"name": "bar"}) \
           .update({"name": "bar2"}).run(conn)
    assert res["errors"] == 0
    wait_until(lambda: check_tables_named(["foo", "bar2"]))

    print "Testing that we can't rename a table so as to cause a name collision..."
    res = r.db("rethinkdb").table("table_config").filter({"name": "bar2"}) \
           .update({"name": "foo"}).run(conn)
    assert res["errors"] == 1

    cluster1.check_and_stop()
print "Done."

