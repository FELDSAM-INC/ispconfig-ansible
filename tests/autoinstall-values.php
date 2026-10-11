<?php
/*
 * roles/ispconfig_server/files/autoinstall.php, which the patched ISPConfig autoinstaller requires in place of its
 * generated autoinstall.ini. Run: php tests/autoinstall-values.php (exit status 1 on a failed check).
 */

// The relevant part of the autoinstall.ini the autoinstaller generates (ISPConfigDebianOS::runPerfectSetup).
const GENERATED = "[install]\n\tlanguage=en\n\tinstall_mode=expert\n\thostname=web1.example.com\n\tispconfig_port=8080\n"
    ."\tispconfig_admin_password=generated\n\n\t[ssl_cert]\n\tssl_cert_country=DE\n\n\t[expert]\n\tmysql_ispconfig_user=ispconfig\n"
    ."\tjoin_multiserver_setup=n\n\tmysql_master_hostname=\n\tmysql_master_root_password=\n\tinstall_ispconfig_web_interface=y\n\n"
    ."\t[update]\n\tmysql_master_hostname=\n\tmysql_master_root_password=\n";

$failures = 0;
function check(bool $ok, string $what): void
{
    global $failures;
    echo ($ok ? 'ok   ' : 'FAIL ').$what."\n";
    $failures += $ok ? 0 : 1;
}

function apply(array $values, string $autoinstall = GENERATED): string
{
    $dir = sys_get_temp_dir().'/ispcp-ai-'.bin2hex(random_bytes(4));
    mkdir($dir);
    copy(dirname(__DIR__).'/roles/ispconfig_server/files/autoinstall.php', $dir.'/autoinstall.php');
    file_put_contents($dir.'/autoinstall.json', json_encode($values));
    try {
        return (static function (string $file, string $autoinstall): string {
            return require $file;
        })($dir.'/autoinstall.php', $autoinstall);
    } finally {
        array_map('unlink', glob($dir.'/*'));
        rmdir($dir);
    }
}

function refused(array $values, string $message, string $autoinstall = GENERATED): bool
{
    try {
        apply($values, $autoinstall);
    } catch (Exception $e) {
        return strpos($e->getMessage(), $message) === 0;
    }

    return false;
}

$result = apply([
    'join_multiserver_setup' => 'y',
    'mysql_master_hostname' => '10.0.0.10',
    'mysql_master_port' => '3306',
    'mysql_master_root_password' => 'p=a$s\\1;#"x',
    'install_ispconfig_web_interface' => 'n',
    'ssl_cert_country' => 'CZ',
]);
check(strpos($result, "\t[expert]\n\tmysql_master_port=3306\n\tmysql_ispconfig_user=ispconfig\n\tjoin_multiserver_setup=y\n") !== false,
    'a missing key is added to [expert], an existing one replaced');
check(substr_count($result, "\tmysql_master_hostname=10.0.0.10\n") === 2, 'a key is replaced in every section ([expert] and [update])');
check(substr_count($result, "\tmysql_master_root_password=p=a\$s\\1;#\"x\n") === 2, 'values with $, \\, ;, # and quotes stay verbatim');
check(strpos($result, "\tinstall_ispconfig_web_interface=n\n") !== false && strpos($result, "\tssl_cert_country=CZ\n") !== false, 'other keys are replaced');
check(strpos($result, "\tispconfig_admin_password=generated\n") !== false, 'keys without a value stay untouched');
foreach ([['k' => "a\nb"], ['k' => ' padded'], ['Bad-Key' => 'x']] as $values) {
    check(refused($values, 'Invalid autoinstall value'), 'refuses '.json_encode($values));
}
check(refused(['new_key' => 'x'], 'The generated autoinstall.ini has no [expert] section', "[install]\nlanguage=en\n"), 'refuses an ini without [expert]');

exit($failures === 0 ? 0 : 1);
