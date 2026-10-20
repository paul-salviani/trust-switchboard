# Copy A2A onto the EdgeOS droplet and run remote_install.sh.
# Requires ~/.ssh/a2a_ed25519 already in root authorized_keys.
$ErrorActionPreference = "Stop"
$Key = Join-Path $env:USERPROFILE ".ssh\a2a_ed25519"
$HostName = "170.64.146.185"
$Root = Split-Path $PSScriptRoot -Parent
if (-not (Test-Path $Key)) { throw "missing $Key" }

$ssh = @("-i", $Key, "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=accept-new")
ssh @ssh "root@$HostName" "mkdir -p /opt/a2a/public /opt/a2a/deploy /opt/a2a/tests /opt/a2a/client"

$files = @(
    "server.py", "db.py", "auth.py", "policy.py", "tape.py", "uniqueness.py",
    "recommend.py", "geo.py", "mcp.py", "envfile.py", "handshake.py",
    "hitl.py", "spend.py", "nutrition.py", "policy_evidence.py",
    "schema.sql", "seed.json", "catalog.json"
)
foreach ($f in $files) {
    scp @ssh (Join-Path $Root $f) "root@${HostName}:/opt/a2a/$f"
}
Get-ChildItem (Join-Path $Root "public") -File | ForEach-Object {
    scp @ssh $_.FullName "root@${HostName}:/opt/a2a/public/$($_.Name)"
}
scp @ssh (Join-Path $Root "client\trust_switch_client.py") "root@${HostName}:/opt/a2a/client/trust_switch_client.py"
Get-ChildItem (Join-Path $Root "deploy") -File | ForEach-Object {
    scp @ssh $_.FullName "root@${HostName}:/opt/a2a/deploy/$($_.Name)"
}

ssh @ssh "root@$HostName" "chmod +x /opt/a2a/deploy/remote_install.sh /opt/a2a/deploy/new_write_key.py; bash /opt/a2a/deploy/remote_install.sh"
python (Join-Path $Root "deploy\smoke_local.py") "http://${HostName}:8787"
