#!/bin/bash
# Run against the staged CLI before replacing an installed PHP. Requires only Bash and PHP 5.3.
# PHP variables in the snippets must reach PHP without shell expansion.
# shellcheck disable=SC2016
set -euo pipefail
PHP53=${1:?PHP 5.3 CLI binary}

fail() { echo "PHP 5.3 security check failed: $*" >&2; exit 1; }

actual=$("$PHP53" -n -r '
if (PHP_VERSION !== "5.3.29" || ini_get("max_multipart_body_parts") !== "-1") exit(1);
$a = "one"; $a .= "two"; $b = $a . "three"; $c = "$a/$b";
if ($c !== "onetwo/onetwothree") exit(1);
parse_str("a[b][c]=ok&list[]=one&list[]=two", $vars);
if ($vars["a"]["b"]["c"] !== "ok" || count($vars["list"]) !== 2) exit(1);
register_shutdown_function(function () { echo "normal-shutdown"; });
') || fail "normal execution or backported INI setting"
[[ $actual == normal-shutdown ]] || fail "normal shutdown callback"

# Freeing a reserve makes an unpatched shutdown handler observable after OOM.
# Error callbacks, destructors and output handlers must also be skipped on hard failure.
setup='
$reserve = str_repeat("r", 1024 * 1024);
register_shutdown_function(function () { $GLOBALS["reserve"] = NULL; echo "UNSAFE-shutdown"; });
set_error_handler(function () { echo "UNSAFE-error-handler"; });
class SecurityProbe { function __destruct() { echo "UNSAFE-destructor"; } }
$probe = new SecurityProbe;
ob_start(function ($buffer) { echo "UNSAFE-output-handler"; return $buffer; });
echo "UNSAFE-buffer-flush";
'
for scenario in allocation reallocation overflow; do
    case $scenario in
        allocation) trigger='str_repeat("x", 32 * 1024 * 1024);'; expected='Allowed memory size';;
        reallocation) trigger='$s = str_repeat("x", 1024 * 1024); while (true) { $s .= $s; }'; expected='Allowed memory size';;
        overflow) trigger='str_repeat("abc", PHP_INT_MAX);'; expected='Possible integer overflow';;
    esac
    status=0
    output=$("$PHP53" -n -d memory_limit=16M -r "$setup $trigger" 2>&1) || status=$?
    [[ $status == 1 ]] || fail "$scenario exited with $status instead of 1: $output"
    [[ $output == *"Fatal error: $expected"* ]] || fail "$scenario diagnostic: $output"
    [[ $output != *UNSAFE-* ]] || fail "$scenario executed shutdown code: $output"
done
echo 'PHP 5.3 security checks passed (normal execution, OOM, realloc OOM, allocation overflow)'
