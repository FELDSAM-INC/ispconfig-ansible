# BIND secondaries for ISPConfig catalog zones

[`secondary.yml`](secondary.yml) configures Debian/Ubuntu secondaries with BIND 9.18+ for the catalog zones of
ISPConfig DNS servers that run the [dns-catalog server tool](https://github.com/FELDSAM-INC/ispconfig-rest/blob/main/dns-catalog/README.md). It reads each
server's catalog name and key with `ispconfig-dns-catalog export`, registers the secondaries for NOTIFY there, installs
BIND, writes the keys (root:bind 0640), the catalog zones and the `catalog-zones` block of `named.conf.options`, checks
the configuration and reloads BIND. Running it again changes nothing; it also adds new primaries or secondaries.

```sh
cd dns-secondary
cp inventory.example.yml inventory.yml     # list the ISPConfig DNS servers and the secondaries
ansible-playbook -i inventory.yml secondary.yml
```

[`site.yml`](../site.yml) runs it at the end when the inventory has hosts in `ispconfig_dns_secondaries`, with
`ispconfig_dns_primaries` as a child group of `ispconfig_dns`.

The playbook needs root (`become`) on both groups and Ansible 2.15 or newer. Variables:

| Variable | Host group | Default | Meaning |
| --- | --- | --- | --- |
| `ispcp_primary_addresses` | primaries | the host's IPv4 address | addresses the secondaries transfer from (IP addresses, not names) |
| `ispcp_secondary_addresses` | secondaries | the host's IPv4 address | addresses the primaries notify |
| `ispcp_install_packages` | secondaries | `true` | install `bind9` |
| `ispcp_manage_service` | secondaries | `true` | enable and start `named` |

The dns-catalog guide describes the catalog zone, setting up a secondary by hand and how to check it.
