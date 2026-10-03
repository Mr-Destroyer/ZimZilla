"""Network scope guard for bug-bounty engagements.

An optional ``scope.yaml`` lists the domains/IPs/CIDRs the engagement may
touch. When a scope file is loaded, any bash command that appears to reach a
host outside that allow-list is hard-blocked before it runs.

Semantics
---------
* No scope file at all  -> guard inactive (plain coding session).
* Scope file present but empty (no domains/CIDRs/ips) -> block ALL network
  targets. An empty scope means "nothing is authorised", never "everything".
* Scope file present and populated -> only listed hosts/CIDRs are reachable;
  everything else is blocked.

The detector deliberately does not attempt to run the shell. It tokenises the
command, pulls out URLs, ``host:port`` pairs and bare host/IP literals, and
checks each against the allow-list. It also inspects common tools (curl, nmap,
ffuf, dig, ssh, ...) for target-shaped arguments.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
except Exception:  # pragma: no cover - yaml is a hard dep, but stay importable
    yaml = None


# Tools whose arguments commonly name a network target.
NETWORK_TOOLS = {
    "curl", "wget", "nmap", "ffuf", "gobuster", "feroxbuster", "nikto",
    "masscan", "nc", "netcat", "ncat", "ssh", "scp", "sftp", "socat",
    "dig", "host", "nslookup", "whois", "traceroute", "tracepath", "ping",
    "hydra", "sqlmap", "wfuzz", "dirb", "dnsrecon", "amass", "subfinder",
    "httpx", "httprobe", "nuclei", "whatweb", "wpscan", "smbclient",
    "redis-cli", "mongo", "psql", "mysql", "telnet", "openssl", "arjun",
    "katana", "gau", "waybackurls", "dalfox", "commix", "xsser",
}

# Shell tokens that are never targets.
_SHELL_NOISE = {
    "|", "||", "&&", "&", ";", ">", ">>", "<", "2>", "2>>", "(", ")",
    "sudo", "env", "time", "timeout", "xargs", "watch", "do", "then",
}

_URL_RE = re.compile(r"\b([a-zA-Z][a-zA-Z0-9+.-]*)://([^\s'\"|;&)]+)")
_HOSTPORT_RE = re.compile(r"^([a-zA-Z0-9_.\-]+):(\d{1,5})$")
_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_IPV4_CIDR_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}/\d{1,2}$")
# An optional trailing dot is the FQDN root: `evil.com.` resolves to `evil.com`,
# so it must be recognised as the same host rather than slipping past the guard.
_HOSTNAME_RE = re.compile(r"^(?!-)[a-zA-Z0-9-]{1,63}(?:\.[a-zA-Z0-9-]{1,63})+\.?$")

# Shell substitution/expansion: the value is evaluated at runtime and never
# appears as a static token, so a host can be built dynamically. We cannot
# resolve it, so a network-tool command whose *only* targets are dynamic is
# treated as unresolvable and blocked (a command that also names a literal
# target is checked against that literal as usual).
_SUBSTITUTION_RE = re.compile(r"\$\(|\$\{|\$[A-Za-z_]|`")

# Tokens that look like flags or values, not hosts.
_FLAG_RE = re.compile(r"^--?[A-Za-z]")
_VERSIONISH_RE = re.compile(r"^\d+(\.\d+)*$")

# A bare dotted token (``evil.com``) is ambiguous: it may be a hostname or a
# filename (``report.json``). To avoid hard-blocking ordinary commands, a bare
# hostname only counts as a target when EITHER the command runs a known network
# tool, OR its final label is a TLD that is never a file extension. URLs,
# host:port pairs, IPs and CIDRs are always treated as targets.
_NEVER_A_FILE_EXT_TLDS = {
    "com", "net", "org", "io", "co", "dev", "app", "cloud", "xyz", "online",
    "site", "tech", "store", "info", "biz", "me", "tv", "cc", "ru", "cn",
    "uk", "de", "fr", "jp", "au", "ca", "nl", "eu", "us", "gov", "edu",
    "mil", "ai", "gg", "to", "name", "pro", "mobi", "asia", "ac", "am",
    "br", "ch", "es", "fi", "gr", "hk", "id", "ie", "il", "it", "kr",
    "mx", "my", "no", "nz", "ph", "pl", "pt", "ro", "se", "sg", "tr",
    "tw", "ua", "vn", "za",
}

# Shell metacharacters that split a command into independently-executed pieces.
_PIPE_SPLIT_RE = re.compile(r"(?:\|\||&&|[|;&\n])")
# Commands that prefix the real one without changing which binary runs.
_PREFIX_WORDS = {"sudo", "env", "time", "timeout", "nohup", "command", "xargs", "watch"}


@dataclass
class ScopeViolation:
    target: str
    reason: str

    def __str__(self) -> str:
        return f"{self.target} — {self.reason}"


@dataclass
class Scope:
    """A loaded scope definition and its allow-list."""

    path: Path | None = None
    domains: set[str] = field(default_factory=set)
    ips: set[str] = field(default_factory=set)
    cidrs: list = field(default_factory=list)  # ipaddress networks
    loaded: bool = False

    # ---- construction -----------------------------------------------------
    @classmethod
    def load(cls, path: Path | None) -> "Scope":
        if path is None:
            return cls(loaded=False)
        path = Path(path).expanduser()
        if not path.exists():
            return cls(loaded=False)
        data = {}
        if yaml is not None:
            try:
                data = yaml.safe_load(path.read_text()) or {}
            except Exception:
                data = {}
        if not isinstance(data, dict):
            data = {}
        scope = cls(path=path, loaded=True)
        scope._ingest(data)
        return scope

    def _ingest(self, data: dict) -> None:
        def collect(value) -> list[str]:
            if value is None:
                return []
            if isinstance(value, str):
                return [value]
            if isinstance(value, (list, tuple, set)):
                return [str(v) for v in value]
            return []

        for entry in collect(data.get("domains")) + collect(data.get("in_scope")):
            host = self._normalise_host(entry)
            if host:
                self.domains.add(host)

        for entry in collect(data.get("ips")) + collect(data.get("hosts")):
            entry = entry.strip()
            if _IPV4_CIDR_RE.match(entry) or ":" in entry and "/" in entry:
                try:
                    self.cidrs.append(ipaddress.ip_network(entry, strict=False))
                    continue
                except ValueError:
                    pass
            if _IPV4_RE.match(entry):
                self.ips.add(entry)
            else:
                host = self._normalise_host(entry)
                if host:
                    self.domains.add(host)

        for entry in collect(data.get("cidrs")) + collect(data.get("networks")):
            try:
                self.cidrs.append(ipaddress.ip_network(entry.strip(), strict=False))
            except ValueError:
                continue

    @staticmethod
    def _normalise_host(entry: str) -> str:
        entry = entry.strip().lower()
        if not entry:
            return ""
        entry = entry.split("://")[-1]
        entry = entry.split("/")[0]
        entry = entry.split(":")[0]
        entry = entry.strip("*.").strip(".")
        return entry

    # ---- queries ----------------------------------------------------------
    @property
    def empty(self) -> bool:
        return not (self.domains or self.ips or self.cidrs)

    def allows(self, host: str) -> tuple[bool, str]:
        """Return (allowed, reason) for a host/IP."""
        if not self.loaded:
            return True, "no scope file"
        host = host.strip().lower().strip("[]").rstrip(".")
        if not host:
            return True, "empty"
        if host == "<command substitution>":
            return False, "unresolvable target (command substitution)"

        # Loopback/localhost is always considered infrastructure, not a target.
        if host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}:
            return True, "local"

        # IP literal?
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None

        if ip is not None:
            if str(ip) in self.ips:
                return True, "listed ip"
            for net in self.cidrs:
                if ip.version == net.version and ip in net:
                    return True, f"in {net}"
            if self.empty:
                return False, "scope file is empty — no targets authorised"
            return False, "ip not in scope"

        # Hostname: exact, or subdomain of a listed domain.
        if host in self.domains:
            return True, "listed domain"
        for dom in self.domains:
            if host == dom or host.endswith("." + dom):
                return True, f"subdomain of {dom}"
        if self.empty:
            return False, "scope file is empty — no targets authorised"
        return False, "host not in scope"

    # ---- scanning ---------------------------------------------------------
    def check_command(self, command: str) -> list[ScopeViolation]:
        """Return a list of violations; empty means the command is allowed."""
        if not self.loaded:
            return []

        violations: list[ScopeViolation] = []
        seen: set[str] = set()

        # Runtime command substitution builds a target the token scan can never
        # see. If the command runs a network tool and contains $() or backticks,
        # the target is unresolvable — treat it as out of scope.
        targets = self.extract_targets(command)
        if (
            not targets
            and _SUBSTITUTION_RE.search(command)
            and self.command_uses_network_tool(command)
        ):
            targets = ["<command substitution>"]

        for host in targets:
            if host in seen:
                continue
            seen.add(host)
            allowed, reason = self.allows(host)
            if not allowed:
                violations.append(ScopeViolation(host, reason))
        return violations

    def extract_targets(self, command: str, network_context: bool | None = None) -> list[str]:
        """Best-effort extraction of network targets from a shell command.

        Unambiguous targets (URLs, host:port, IPs, CIDRs) are always returned.
        Bare dotted hostnames are only returned when the command runs a known
        network tool or the final label is a domain-only TLD — otherwise a
        filename like ``report.json`` would be mistaken for a host.
        """
        if network_context is None:
            network_context = self.command_uses_network_tool(command)

        targets: list[str] = []

        # 1. Explicit URLs — unambiguous.
        for _scheme, rest in _URL_RE.findall(command):
            host = rest.split("/")[0].split("?")[0].split("@")[-1]
            host = host.split(":")[0]
            if host:
                targets.append(host)

        # 2. Token scan.
        for raw in re.split(r"\s+", command):
            tok = raw.strip().strip("'\"`,")
            if not tok or tok in _SHELL_NOISE:
                continue
            if _FLAG_RE.match(tok):
                # Flags can still embed a URL (e.g. --url=https://x)
                if "=" in tok:
                    val = tok.split("=", 1)[1]
                    m = _URL_RE.match(val) or _URL_RE.search(val)
                    if m:
                        h = m.group(2).split("/")[0].split(":")[0]
                        if h:
                            targets.append(h)
                continue

            m = _HOSTPORT_RE.match(tok)
            if m:
                targets.append(m.group(1))
                continue

            if _IPV4_CIDR_RE.match(tok):
                targets.append(tok.split("/")[0])
                continue

            if _IPV4_RE.match(tok):
                targets.append(tok)
                continue

            if "://" in tok:
                h = tok.split("://", 1)[1].split("/")[0].split(":")[0]
                if h:
                    targets.append(h)
                continue

            if _HOSTNAME_RE.match(tok) and not _VERSIONISH_RE.match(tok):
                # `evil.com.` is the same host as `evil.com`; strip the root dot
                # before the tail/TLD checks so the FQDN form can't slip through.
                bare = tok.rstrip(".")
                tail = bare.rsplit(".", 1)[-1].lower()
                if not (tail.isalpha() and len(tail) >= 2):
                    continue
                # Skip obvious filenames: has a directory part, starts with a
                # path marker, or the tail is a file extension rather than a TLD.
                if "/" in tok or "\\" in tok or tok.startswith("."):
                    continue
                if network_context or tail in _NEVER_A_FILE_EXT_TLDS:
                    targets.append(bare)
        return targets

    @staticmethod
    def command_uses_network_tool(command: str) -> bool:
        """True if any segment of *command* invokes a known network binary."""
        for segment in _PIPE_SPLIT_RE.split(command):
            words = [w for w in segment.strip().split() if w]
            # Strip leading assignments (FOO=bar cmd) and prefix words.
            while words and ("=" in words[0] and not words[0].startswith("-")):
                words.pop(0)
            while words and words[0] in _PREFIX_WORDS:
                words.pop(0)
            if not words:
                continue
            binary = words[0].rsplit("/", 1)[-1]
            if binary in NETWORK_TOOLS:
                return True
        return False

    def describe(self) -> str:
        if not self.loaded:
            return "guard off (no scope file)"
        if self.empty:
            return "scope EMPTY — all network blocked"
        n = len(self.domains) + len(self.ips) + len(self.cidrs)
        return f"{n} entries ({len(self.domains)}d/{len(self.ips)}i/{len(self.cidrs)}n)"
