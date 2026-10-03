"""Target scope for bug-bounty engagements: ``allow.yaml`` + ``out-of-scope.yaml``.

Two files, one job each:

``allow.yaml``
    The targets the operator has declared and is authorised to test. Its
    **presence arms the session** — every gated tool (bash, write_file,
    edit_file) then runs without asking, the same way /mode danger does. The
    list itself is declarative: it is put in the system prompt and shown in the
    UI so the model knows what it is cleared for, but it does **not** restrict
    anything. A host that is not listed is not blocked by this file.

``out-of-scope.yaml``
    Hosts that must never be touched. Any bash command that appears to reach a
    host listed here is hard-blocked before it runs. Deny always beats allow.

Semantics
---------
* Neither file present      -> nothing armed, nothing blocked.
* ``allow.yaml`` present    -> session armed (tools run unattended).
* ``out-of-scope.yaml`` present -> every host it lists is refused, always,
  regardless of what ``allow.yaml`` says.

The guard is a *static text scan*, not a sandbox: it tokenises the command,
pulls out URLs, ``host:port`` pairs and bare host/IP literals, and checks each
against the deny-list. It does not run the shell, resolve DNS, or observe what
a process does after it starts. See the README for what that does and does not
cover.
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

# The spans those substitutions occupy, removed whole. Used to answer "does this
# command name any *literal* target?" — the raw text cannot answer it, because a
# token like `$(cat` is captured by the URL regex as if it were a hostname, which
# would make a purely dynamic command look like it had a static target.
_SUBSTITUTION_SPAN_RE = re.compile(
    r"\$\([^)]*\)"          # $(command)
    r"|\$\{[^}]*\}"         # ${var}
    r"|\$[A-Za-z_][A-Za-z0-9_]*"  # $var
    r"|`[^`]*`"             # `command`
)


def _strip_substitutions(command: str) -> str:
    """*command* with every substitution span removed.

    What remains is the static skeleton: the parts that are known before the
    shell runs. Extracting targets from this — rather than from the raw text —
    is what lets a command whose only target is dynamic be recognised as having
    no target at all.
    """
    return _SUBSTITUTION_SPAN_RE.sub(" ", command)

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

# YAML keys accepted for each kind of entry. Singular is the documented form;
# the plural and the older `scope.yaml` spellings are accepted so an existing
# file keeps parsing.
_DOMAIN_KEYS = ("domain", "domains", "in_scope")
_IP_KEYS = ("ip", "ips", "hosts")
_CIDR_KEYS = ("cidr", "cidrs", "networks")


@dataclass
class ScopeViolation:
    target: str
    reason: str

    def __str__(self) -> str:
        return f"{self.target} — {self.reason}"


@dataclass
class HostSet:
    """A parsed set of domains, IPs and CIDRs, and the matching rules for it."""

    domains: set[str] = field(default_factory=set)
    ips: set[str] = field(default_factory=set)
    cidrs: list = field(default_factory=list)  # ipaddress networks

    @classmethod
    def from_mapping(cls, data: dict) -> "HostSet":
        hs = cls()
        if not isinstance(data, dict):
            return hs

        def collect(value) -> list[str]:
            if value is None:
                return []
            if isinstance(value, str):
                return [value]
            if isinstance(value, (list, tuple, set)):
                return [str(v) for v in value]
            return []

        for key in _DOMAIN_KEYS:
            for entry in collect(data.get(key)):
                host = cls._normalise_host(entry)
                if host:
                    hs.domains.add(host)

        for key in _IP_KEYS:
            for entry in collect(data.get(key)):
                entry = entry.strip()
                if _IPV4_CIDR_RE.match(entry) or (":" in entry and "/" in entry):
                    try:
                        hs.cidrs.append(ipaddress.ip_network(entry, strict=False))
                        continue
                    except ValueError:
                        pass
                if _IPV4_RE.match(entry):
                    hs.ips.add(entry)
                else:
                    host = cls._normalise_host(entry)
                    if host:
                        hs.domains.add(host)

        for key in _CIDR_KEYS:
            for entry in collect(data.get(key)):
                try:
                    hs.cidrs.append(ipaddress.ip_network(entry.strip(), strict=False))
                except ValueError:
                    continue
        return hs

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

    @property
    def empty(self) -> bool:
        return not (self.domains or self.ips or self.cidrs)

    def __len__(self) -> int:
        return len(self.domains) + len(self.ips) + len(self.cidrs)

    def contains(self, host: str) -> tuple[bool, str]:
        """Return (is_member, reason) for a host/IP against this set.

        Polarity-neutral on purpose: membership means "permitted" for an
        allow-list and "blocked" for a deny-list, so callers decide what a
        match implies. Keeping this neutral is what stops the two lists from
        silently inverting each other.
        """
        host = host.strip().lower().strip("[]").rstrip(".")
        if not host:
            return False, "empty"

        # Loopback/localhost is always considered infrastructure, not a target,
        # so it is never a member of either list.
        if host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}:
            return False, "local"

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
            return False, "ip not listed"

        # Hostname: exact, or subdomain of a listed domain.
        if host in self.domains:
            return True, "listed domain"
        for dom in self.domains:
            if host == dom or host.endswith("." + dom):
                return True, f"subdomain of {dom}"
        return False, "host not listed"

    def describe(self) -> str:
        if self.empty:
            return "empty"
        return (
            f"{len(self)} entries "
            f"({len(self.domains)}d/{len(self.ips)}i/{len(self.cidrs)}n)"
        )


@dataclass
class Scope:
    """The session's target scope: an allow-list and a deny-list."""

    allow_path: Path | None = None
    deny_path: Path | None = None
    allow: HostSet = field(default_factory=HostSet)
    deny: HostSet = field(default_factory=HostSet)
    #: Set when a legacy `scope.yaml` was found, so the UI can say so once.
    legacy_path: Path | None = None

    # ---- construction -----------------------------------------------------
    @classmethod
    def load(cls, allow_path: Path | None, deny_path: Path | None) -> "Scope":
        scope = cls()
        if allow_path is not None:
            path = Path(allow_path).expanduser()
            if path.exists():
                scope.allow_path = path
                scope.allow = HostSet.from_mapping(_read_yaml(path))
        if deny_path is not None:
            path = Path(deny_path).expanduser()
            if path.exists():
                scope.deny_path = path
                scope.deny = HostSet.from_mapping(_read_yaml(path))
        return scope

    # ---- queries ----------------------------------------------------------
    @property
    def armed(self) -> bool:
        """True when allow.yaml is present: tools run without confirmation."""
        return self.allow_path is not None

    @property
    def loaded(self) -> bool:
        """True when either file is present."""
        return self.allow_path is not None or self.deny_path is not None

    def in_allow(self, host: str) -> tuple[bool, str]:
        """Whether *host* is one of the operator's declared targets."""
        if not self.armed:
            return False, "no allow.yaml"
        return self.allow.contains(host)

    def allows(self, host: str) -> tuple[bool, str]:
        """Whether *host* may be reached: not deny-listed. Deny beats allow."""
        if not self.loaded:
            return True, "no scope files"
        denied, reason = self.deny.contains(host)
        if denied:
            return False, f"denied — {reason}"
        return True, "not denied"

    # ---- scanning ---------------------------------------------------------
    def check_command(self, command: str) -> list[ScopeViolation]:
        """Return the deny-list violations for *command*; empty means allowed."""
        if self.deny_path is None:
            return []

        violations: list[ScopeViolation] = []
        seen: set[str] = set()

        # Runtime command substitution builds a target the token scan can never
        # see. If the command runs a network tool and names no *literal* target,
        # the target is unresolvable — fail closed and refuse the command.
        # Handled here rather than in HostSet.contains because membership is
        # polarity-neutral: only the deny-list has a reason to fail closed.
        #
        # The literal check runs against the substitution-stripped skeleton, not
        # the raw text: in `curl https://$(cat t)/` the URL regex happily
        # captures `$(cat` as a "host", so a raw scan would report a target and
        # let the command through.
        targets = self.extract_targets(command)
        literal = self.extract_targets(_strip_substitutions(command))
        if (
            not literal
            and _SUBSTITUTION_RE.search(command)
            and self.command_uses_network_tool(command)
        ):
            return [
                ScopeViolation(
                    "<command substitution>",
                    "unresolvable target (command substitution)",
                )
            ]

        for host in targets:
            if host in seen:
                continue
            seen.add(host)
            denied, reason = self.deny.contains(host)
            if denied:
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

    # ---- presentation -----------------------------------------------------
    def describe(self) -> str:
        if not self.loaded:
            return "off — no scope files"
        bits = []
        if self.armed:
            bits.append(f"allow: {self.allow.describe()}")
        else:
            bits.append("allow: none")
        if self.deny_path is not None:
            bits.append(f"deny: {self.deny.describe()}")
        return " · ".join(bits)

    def badge(self) -> str:
        """One short string for the header."""
        if self.armed and self.deny_path is not None:
            return f"ARMED + deny {len(self.deny)}"
        if self.armed:
            return "ARMED"
        if self.deny_path is not None:
            return f"deny {len(self.deny)}"
        return "off"


def _read_yaml(path: Path) -> dict:
    if yaml is None:
        return {}
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def find_legacy_scope(workdir: Path, home: Path | None = None) -> Path | None:
    """A leftover ``scope.yaml``, which no longer means anything.

    The old file was an allow-list that also blocked; reinterpreting it as a
    deny-list would block the very hosts the operator had declared. So it is
    never read — only reported, once, so the split is not silent.
    """
    for name in ("scope.yaml", "scope.yml"):
        candidate = Path(workdir) / name
        if candidate.exists():
            return candidate
    if home is not None:
        candidate = Path(home) / ".zimzilla" / "scope.yaml"
        if candidate.exists():
            return candidate
    return None
