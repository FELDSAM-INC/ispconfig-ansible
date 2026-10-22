#!/usr/bin/env python3
"""Exercise the backport over FastCGI with one reused worker; no web server required.

Usage: python3 tests/php53-security.py /path/to/php /path/to/php-fpm
"""
import argparse
import json
import os
from pathlib import Path
import pwd
import socket
import struct
import subprocess
import tempfile
import time


def record(kind, data=b""):
    return struct.pack("!BBHHBB", 1, kind, 1, len(data), 0, 0) + data


def param_length(length):
    return bytes([length]) if length < 128 else struct.pack("!I", length | 0x80000000)


def receive(sock, length):
    data = b""
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise EOFError("worker closed the request")
        data += chunk
    return data


def request(address, script, body=b"", query="", ini="", content_type="multipart/form-data; boundary=security-boundary", diagnostics=None):
    params = {
        "SCRIPT_FILENAME": str(script), "REQUEST_METHOD": "POST" if body else "GET",
        "QUERY_STRING": query, "CONTENT_TYPE": content_type,
        "CONTENT_LENGTH": str(len(body)), "SERVER_PROTOCOL": "HTTP/1.1",
        "GATEWAY_INTERFACE": "CGI/1.1", "REDIRECT_STATUS": "200",
        "PHP_ADMIN_VALUE": ini,
    }
    encoded = b""
    for key, value in params.items():
        key, value = key.encode(), value.encode()
        encoded += param_length(len(key)) + param_length(len(value)) + key + value
    output = b""
    with socket.create_connection(address, timeout=10) as sock:
        sock.sendall(record(1, struct.pack("!HB5x", 1, 0)) + record(4, encoded) + record(4))
        for offset in range(0, len(body), 65535):
            sock.sendall(record(5, body[offset:offset + 65535]))
        sock.sendall(record(5))
        while True:
            _, kind, _, length, padding, _ = struct.unpack("!BBHHBB", receive(sock, 8))
            data = receive(sock, length)
            receive(sock, padding)
            if kind == 6:
                output += data
            elif kind == 7 and diagnostics is not None:
                diagnostics.append(data)
            elif kind == 3:
                return output


def multipart(parts):
    return b"".join(b"--security-boundary\r\n" + headers + b"\r\n\r\n" + body + b"\r\n"
                    for headers, body in parts) + b"--security-boundary--\r\n"


def field(name, value=b"value"):
    return (('Content-Disposition: form-data; name="%s"' % name).encode(), value)


def document(response):
    assert b"STALE-HANDLER" not in response, response
    return json.loads(response.split(b"\r\n\r\n", 1)[1])


def exercise(address, script):
    first = document(request(address, script))
    assert first["version"] == "5.3.29", first
    normal = document(request(address, script, multipart([
        field("nested[a][b]", b"ok"), field("list[]", b"one"), field("list[]", b"two"),
        (b'Content-Disposition: form-data; name="file"; filename="hello.txt"\r\nContent-Type: text/plain', b"hello"),
    ])))
    assert normal["post"] == {"nested": {"a": {"b": "ok"}}, "list": ["one", "two"]}, normal
    assert normal["contents"] == {"file": "hello"}, normal
    assert normal["files"]["file"]["type"] == "text/plain", normal

    # Unknown/duplicate headers and their continuations must not alter retained headers.
    headers = (b'Content-Disposition: form-data; name="file"; filename="good.txt"\r\n'
               b'cOnTeNt-DisPosition: form-data; name="wrong"; filename="bad.txt"\r\n'
               b' ignored continuation\r\nContent-Type: text/plain\r\n'
               b'CONTENT-TYPE: application/invalid\r\n ignored continuation\r\n'
               b'X-Ignored: value\r\n ignored continuation')
    parsed = document(request(address, script, multipart([(headers, b"hello")])))
    assert parsed["files"]["file"]["name"] == "good.txt", parsed
    assert parsed["files"]["file"]["type"] == "text/plain", parsed
    folded = document(request(address, script, multipart([
        (b'Content-Disposition: form-data;\r\n name="folded"', b"yes")
    ])))
    assert folded["post"] == {"folded": "yes"}, folded

    # A large number of ignored headers must not create a list of allocated header entries.
    many_headers = field("header_test")[0] + b"\r\nX-Ignored: x" * 150000
    parsed = document(request(address, script, multipart([(many_headers, b"ok")])))
    assert parsed["post"] == {"header_test": "ok"}, parsed

    for limit, expected in [(0, 0), (1, 1), (2, 2), (3, 3), (-1, 3)]:
        body = multipart([field("f%d" % i) for i in range(3)])
        parsed = document(request(address, script, body, ini="max_multipart_body_parts=%d" % limit))
        assert len(parsed["post"]) == expected, (limit, parsed)

    # Empty uploads do not consume max_file_uploads, but must consume the multipart budget.
    empty_files = [(('Content-Disposition: form-data; name="f%d"; filename=""' % i).encode(), b"")
                   for i in range(4)]
    parsed = document(request(address, script, multipart(empty_files),
                              ini="max_input_vars=1\nmax_file_uploads=1\nmax_multipart_body_parts=-1"))
    assert len(parsed["files"]) == 2, parsed
    assert all(info["error"] == 4 for info in parsed["files"].values()), parsed

    # With one worker, these requests must reuse the same process. A per-request INI
    # diagnostic runs before executor initialization and must not invoke old handlers.
    for mode in ("handlers", "fatal"):
        response = request(address, script, query="mode=" + mode)
        assert b"STALE-HANDLER" not in response, response
        diagnostics = []
        # PHP 5.3 emits E_DEPRECATED for hash-style INI comments before init_executor().
        following = document(request(address, script, ini="# security early diagnostic", diagnostics=diagnostics))
        assert b"Comments starting with '#'" in b"".join(diagnostics), diagnostics
        assert following["pid"] == first["pid"], following

    # Hard OOM must kill the worker without shutdown output; FPM must replace it.
    try:
        response = request(address, script, query="mode=oom")
        assert b"UNSAFE" not in response, response
        raise AssertionError("OOM unexpectedly completed a FastCGI response")
    except (EOFError, ConnectionResetError):
        pass
    following = document(request(address, script))
    assert following["pid"] != first["pid"], following

    # Input parsing can exhaust memory before the application has even started.
    nested = "&".join("f%d%s=value" % (i, "[x]" * 32) for i in range(3000)).encode()
    try:
        request(address, script, nested, ini="max_input_vars=5000",
                content_type="application/x-www-form-urlencoded")
        raise AssertionError("input-parsing OOM unexpectedly completed a response")
    except (EOFError, ConnectionResetError, BrokenPipeError):
        pass
    recovered = document(request(address, script))
    assert recovered["pid"] != following["pid"], recovered
    print("PHP 5.3 FastCGI checks passed (multipart headers/limits, worker reuse, fatal cleanup, runtime/startup OOM)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cli", type=Path)
    parser.add_argument("server", type=Path)
    args = parser.parse_args()
    cli, server = args.cli.resolve(), args.server.resolve()
    repo = Path(__file__).resolve().parents[1]
    subprocess.run(["bash", str(repo / "roles/ispconfig_web/files/verify-php53-security.sh"), str(cli)], check=True)
    with tempfile.TemporaryDirectory(prefix="php53-security-") as temp:
        work = Path(temp)
        work.chmod(0o755)
        script = work / "probe.php"
        script.write_text('''<?php
$mode = isset($_GET['mode']) ? $_GET['mode'] : '';
if ($mode === 'handlers' || $mode === 'fatal') {
    set_error_handler(function () { echo 'STALE-HANDLER'; });
    set_error_handler(function () { echo 'STALE-HANDLER'; });
    set_exception_handler(function ($e) { echo 'STALE-HANDLER'; });
    set_exception_handler(function ($e) { echo 'STALE-HANDLER'; });
    if ($mode === 'fatal') { missing_security_test_function(); }
}
if ($mode === 'oom') {
    register_shutdown_function(function () { echo 'UNSAFE-shutdown'; });
    ob_start(function ($buffer) { return 'UNSAFE-output'; });
    str_repeat('x', 32 * 1024 * 1024);
}
$contents = array();
foreach ($_FILES as $name => $file) {
    if ($file['error'] === 0) { $contents[$name] = file_get_contents($file['tmp_name']); }
}
echo json_encode(array('version' => PHP_VERSION, 'pid' => getmypid(), 'post' => $_POST,
    'files' => $_FILES, 'contents' => $contents));
''')
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            address = sock.getsockname()
        options = ["-n", "-d", "memory_limit=16M", "-d", "display_errors=0", "-d", "log_errors=1", "-d", "error_reporting=32767",
                   "-d", "date.timezone=UTC", "-d", "post_max_size=8M"]
        account = pwd.getpwnam("nobody") if os.geteuid() == 0 else pwd.getpwuid(os.geteuid())
        config = work / "fpm.conf"
        config.write_text("""[global]
daemonize = no
error_log = {log}
[security]
listen = {host}:{port}
user = {user}
group = {group}
pm = static
pm.max_children = 1
pm.max_requests = 0
catch_workers_output = yes
""".format(log=work / "fpm.log", host=address[0], port=address[1], user=account.pw_name, group=account.pw_gid))
        command = [str(server)] + options + ["-y", str(config)]
        with (work / "server.log").open("wb") as log:
            process = subprocess.Popen(command, stdout=log, stderr=log,
                                       env=dict(os.environ, PHP_FCGI_MAX_REQUESTS="1000", PHP_FCGI_CHILDREN="0"))
            try:
                for _ in range(100):
                    if process.poll() is not None:
                        raise RuntimeError("FastCGI server exited during startup")
                    try:
                        with socket.create_connection(address, timeout=0.1):
                            break
                    except OSError:
                        time.sleep(0.05)
                else:
                    raise RuntimeError("FastCGI server did not listen")
                exercise(address, script)
            except Exception:
                print((work / "server.log").read_text())
                if (work / "fpm.log").exists():
                    print((work / "fpm.log").read_text())
                raise
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    main()
