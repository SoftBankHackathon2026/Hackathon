//! Explicit terminal-approved installation. Enrollment secrets and sudo passwords never enter this module.
use crate::{
    crypto,
    network::{self, Policy, Request},
    release, Result,
};
use serde_json::Value;
use std::{
    fs::{self, DirBuilder, File, OpenOptions},
    io::{BufRead, BufReader, Read, Write},
    os::unix::{
        fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt},
        io::AsRawFd,
    },
    path::{Path, PathBuf},
    process::{Command, Stdio},
};
const HELPER: &str = "/usr/local/libexec/migration-net-helper";
const POLICY: &str = "/etc/migration-connector/net-policy.json";
const FLOOR: &str = "/etc/migration-connector/release-sequence";
const SERVICE: &str = "migration-connector.service";
const SERVICE_DIR: &str = "/etc/systemd/system";
const SYSTEMCTL: &str = "/usr/bin/systemctl";
const PACKAGES: &[&str] = &["iproute2", "nftables", "wireguard-tools"];
const MAX_ARTIFACT: u64 = 256 * 1024 * 1024;
const STAGE_SCRIPT: &str = r#"set -eu
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
umask 077
stage=$(/usr/bin/mktemp -d /var/tmp/migration-host-launch.XXXXXXXX)
trap '/usr/bin/rm -rf -- "$stage"' EXIT HUP INT TERM
/usr/bin/install -m 0700 -- "$1" "$stage/migration-connector"
printf '%s  %s\n' "$2" "$stage/migration-connector" | /usr/bin/sha384sum --check --status
shift 2
case "$1" in
    install-host|install-service) "$stage/migration-connector" "$@" ;;
    *) exit 1 ;;
esac
"#;
struct Stage(PathBuf);
impl Stage {
    fn new() -> Result<Self> {
        let p = Path::new("/var/tmp").join(format!(
            "migration-host-{}",
            crypto::b64(&crypto::random::<16>()?)
        ));
        DirBuilder::new().mode(0o700).create(&p)?;
        Ok(Self(p))
    }
}
impl Drop for Stage {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}
fn account(uid: u32, user: &str) -> Result<()> {
    if uid == 0
        || user.is_empty()
        || user.len() > 64
        || !user
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b))
        || user.starts_with('-')
    {
        return Err("설치 대상은 이름이 확인된 일반 사용자여야 합니다.".into());
    }
    let out = Command::new("/usr/bin/id")
        .args(["-u", "--", user])
        .env_clear()
        .output()?;
    if !out.status.success() || String::from_utf8(out.stdout)?.trim().parse::<u32>()? != uid {
        return Err("설치 대상 사용자와 UID가 일치하지 않습니다.".into());
    }
    Ok(())
}
fn read_regular(path: &Path, uid: u32, limit: u64) -> Result<Vec<u8>> {
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)?;
    let m = file.metadata()?;
    if !m.is_file() || m.uid() != uid || m.mode() & 0o022 != 0 || m.len() > limit {
        return Err("설치 파일의 소유권·권한·크기가 올바르지 않습니다.".into());
    }
    let mut b = Vec::new();
    file.take(limit + 1).read_to_end(&mut b)?;
    if b.len() as u64 > limit {
        return Err("설치 파일 크기 제한을 넘었습니다.".into());
    }
    Ok(b)
}
fn create(path: &Path, b: &[u8], mode: u32) -> Result<()> {
    let mut f = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(mode)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)?;
    f.write_all(b)?;
    f.set_permissions(fs::Permissions::from_mode(mode))?;
    f.sync_all()?;
    Ok(())
}
fn safe_name(n: &str) -> bool {
    !n.is_empty()
        && n.len() <= 128
        && n != "."
        && n != ".."
        && n.as_bytes()[0].is_ascii_alphanumeric()
        && n.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
}
fn policy(b: &[u8], uid: u32) -> Result<Policy> {
    let mut p: Policy = serde_json::from_slice(b)?;
    p.validate()?;
    if p.role != "client" || (p.uid != 0 && p.uid != uid) {
        return Err("서명된 정책이 이 단말 사용자용이 아닙니다.".into());
    }
    p.uid = uid;
    Ok(p)
}
fn required(m: &release::Manifest) -> Result<()> {
    for n in [
        "migration-connector",
        "migration-net-helper",
        "net-policy.json",
    ] {
        if !m.artifacts.iter().any(|a| a.name == n) {
            return Err(
                "자동 설치 파일이 서명된 배포 목록에 없습니다. 관리자에게 새 배포를 요청해 주세요."
                    .into(),
            );
        }
    }
    Ok(())
}
fn check(helper: &Path, uid: u32) -> Result<()> {
    for path in [
        "/",
        "/usr",
        "/usr/local",
        "/usr/local/libexec",
        "/etc",
        "/etc/migration-connector",
    ] {
        trusted_directory(Path::new(path))?;
    }
    let m = fs::symlink_metadata(helper)?;
    if !m.is_file() || m.uid() != 0 || m.mode() & 0o022 != 0 || m.mode() & 0o111 == 0 {
        return Err("관리자 소유 네트워크 보조 프로그램이 필요합니다.".into());
    }
    let req = Request {
        operation: "check".into(),
        connection_id: "preflight0000000000".into(),
        epoch: 0,
        private_key: None,
        public_key: None,
        psk: None,
        key_expires_at_ms: 0,
    };
    let v = network::helper(helper, &req)?;
    let p: Policy = serde_json::from_value(v["policy"].clone())?;
    p.validate()?;
    if v["ready"] != true || p.uid != uid || p.role != "client" {
        return Err("보조 프로그램의 사용자·역할·실행 권한을 확인하지 못했습니다.".into());
    }
    Ok(())
}
/// Called after proof and web approval; package downloads do not consume the invitation window.
pub fn ensure(bundle: &Path, helper: &Path) -> Result<()> {
    if helper != Path::new(HELPER) {
        return Err("허용하지 않는 보조 프로그램 경로입니다.".into());
    }
    let uid = unsafe { libc::getuid() };
    if check(helper, uid).is_ok() {
        let manifest = release::verify_bundle(bundle)?;
        if let Some(artifact) = manifest
            .artifacts
            .iter()
            .find(|a| a.name == "migration-net-helper")
        {
            if crypto::digest(&read_regular(helper, 0, MAX_ARTIFACT)?) == artifact.sha384 {
                return Ok(());
            }
        } else {
            return Ok(());
        }
    }
    if uid == 0 {
        return Err("일반 사용자로 실행한 뒤 설치 단계에서 sudo 승인을 해 주세요.".into());
    }
    let user = current_user(uid)?;
    let (manifest, executable, digest) = signed_installer(bundle, uid)?;
    required(&manifest)?;
    let p = policy(
        &read_regular(&bundle.join("net-policy.json"), uid, 65536)?,
        uid,
    )?;
    let mut tty = OpenOptions::new()
        .read(true)
        .write(true)
        .open("/dev/tty")
        .map_err(|_| "관리자 승인 입력을 위해 대화형 터미널에서 명령을 실행해 주세요.")?;
    writeln!(
        tty,
        "\n보안 연결에 필요한 단말 구성요소를 관리자 권한으로 설치합니다."
    )?;
    writeln!(
        tty,
        "패키지: iproute2, nftables, wireguard-tools (없는 패키지만 설치)"
    )?;
    writeln!(
        tty,
        "프로그램: {HELPER}\n정책: {POLICY}\n사용자: {user} (UID {uid})"
    )?;
    writeln!(
        tty,
        "인터페이스: {} / 단말 주소: {} / 상대 주소: {} / 연결 대상: {}",
        p.interface, p.local_address, p.peer_address, p.endpoint
    )?;
    writeln!(
        tty,
        "관리자 실행 허용: 이 사용자에게 보조 프로그램 한 개만, 추가 인수 없이 허용"
    )?;
    write!(tty, "설치를 승인하시겠습니까? [y/N]: ")?;
    tty.flush()?;
    let mut answer = String::new();
    let mut reader = BufReader::new(tty.try_clone()?).take(257);
    reader.read_line(&mut answer)?;
    if answer.len() > 256 || !matches!(answer.trim(), "y" | "Y" | "yes" | "YES") {
        return Err(
            "관리자 설치 승인이 취소됐습니다. 설치 후 같은 사용자로 연결을 재개할 수 있습니다."
                .into(),
        );
    }
    writeln!(
        tty,
        "sudo가 관리자 인증을 요청합니다. 비밀번호는 연결 프로그램에 전달되지 않습니다."
    )?;
    approved_install(
        &executable,
        &digest,
        &[
            "install-host".as_ref(),
            bundle.as_os_str(),
            uid.to_string().as_ref(),
            user.as_ref(),
        ],
    )?;
    check(helper, uid)?;
    writeln!(
        tty,
        "단말 구성요소 설치를 확인했습니다. 보안 연결을 이어갑니다."
    )?;
    Ok(())
}
fn current_user(uid: u32) -> Result<String> {
    let out = Command::new("/usr/bin/id")
        .arg("-un")
        .env_clear()
        .output()?;
    if !out.status.success() {
        return Err("단말 사용자 이름을 확인하지 못했습니다.".into());
    }
    let user = String::from_utf8(out.stdout)?.trim().to_owned();
    account(uid, &user)?;
    Ok(user)
}

fn signed_installer(bundle: &Path, uid: u32) -> Result<(release::Manifest, PathBuf, String)> {
    let manifest = release::verify_bundle(bundle)?;
    let executable = std::env::current_exe()?;
    let digest = crypto::digest(&read_regular(&executable, uid, MAX_ARTIFACT)?);
    if !manifest
        .artifacts
        .iter()
        .any(|a| a.name == "migration-connector" && a.sha384 == digest)
    {
        return Err("현재 실행 파일이 서명된 설치 프로그램과 일치하지 않습니다.".into());
    }
    Ok((manifest, executable, digest))
}

fn approved_install(executable: &Path, digest: &str, args: &[&std::ffi::OsStr]) -> Result<()> {
    let status = Command::new("/usr/bin/sudo")
        .args([
            "-p",
            "[migration-admin] password: ",
            "--",
            "/bin/sh",
            "-c",
            STAGE_SCRIPT,
            "migration-host-install",
        ])
        .arg(executable)
        .arg(digest)
        .args(args)
        .env_clear()
        .stdin(Stdio::null())
        .status()?;
    if !status.success() {
        return Err("관리자 설치 또는 서비스 시작을 완료하지 못했습니다.".into());
    }
    Ok(())
}

fn installation_account(uid: u32, user: &str) -> Result<()> {
    account(uid, user)?;
    if unsafe { libc::geteuid() } != 0
        || std::env::var("SUDO_UID").ok().as_deref() != Some(uid.to_string().as_str())
        || std::env::var("SUDO_USER").ok().as_deref() != Some(user)
    {
        return Err("관리자 승인을 한 사용자와 설치 대상이 일치하지 않습니다.".into());
    }
    Ok(())
}

fn unit_arg(value: &str) -> Result<String> {
    if value.chars().any(char::is_control) {
        return Err("서비스 설정의 경로에 제어 문자를 사용할 수 없습니다.".into());
    }
    Ok(format!(
        r#""{}""#,
        value
            .replace('\\', r"\\")
            .replace('"', r#"\""#)
            .replace('%', "%%")
    ))
}

// Preserve local CLI configuration selectors across the service boundary.
// Access keys, tokens and arbitrary environment variables never enter the unit.
const CLOUD_SELECTORS: [&str; 3] = ["AWS_PROFILE", "AWS_REGION", "CLOUDSDK_ACTIVE_CONFIG_NAME"];

fn unit(
    bundle: &Path, uid: u32, request: &str, home: &str, kubeconfig: &str,
    selectors: &[String; 3],
) -> Result<String> {
    crate::api::identifier(request)?;
    if uid == 0
        || !bundle.is_absolute()
        || !Path::new(home).is_absolute()
        || std::env::split_paths(kubeconfig).any(|p| !p.as_os_str().is_empty() && !p.is_absolute())
    {
        return Err("일반 사용자와 절대 경로가 필요합니다.".into());
    }
    let executable = bundle.join("migration-connector");
    let config = bundle.join("connector.json");
    let executable = unit_arg(executable.to_str().ok_or("invalid executable path")?)?;
    // systemd restricts executable names. Pass the user path as an argument to
    // a fixed launcher; exec keeps the same PID for the readiness notification.
    let launcher = unit_arg(r#"exec "$@""#)?;
    let config = unit_arg(config.to_str().ok_or("invalid config path")?)?;
    let state = Path::new(home)
        .join(".local/state/migration-connector")
        .join(request)
        .join("state.json");
    let state = unit_arg(state.to_str().ok_or("invalid state path")?)?;
    let home = unit_arg(&format!("HOME={home}"))?;
    let kubeconfig = unit_arg(&format!("KUBECONFIG={kubeconfig}"))?;
    let mut cloud = String::new();
    for (name, value) in CLOUD_SELECTORS.iter().zip(selectors) {
        if !value.is_empty() {
            if value.len() > 128 {
                return Err("클라우드 명령줄 도구 설정 이름이 너무 깁니다.".into());
            }
            cloud.push(' ');
            cloud.push_str(&unit_arg(&format!("{name}={value}"))?);
        }
    }
    Ok(format!("# migration-connector uid={uid} request={request}\n\
[Unit]\nDescription=On-premises migration tunnel\nStartLimitIntervalSec=10min\nStartLimitBurst=5\nWants=network-online.target\nAfter=network-online.target\n\
[Service]\nType=notify\nNotifyAccess=main\nUser={uid}\nUMask=0077\n\
Environment={home} {kubeconfig}{cloud}\n\
Environment=PATH=/usr/sbin:/usr/bin:/sbin:/bin:/usr/local/sbin:/usr/local/bin\n\
ExecCondition=:/usr/bin/test -f {state}\n\
ExecStart=:/bin/sh -c {launcher} migration-connector-launch {executable} serve {config} {request}\n\
KillMode=mixed\n\
TimeoutStartSec=90s\nTimeoutStopSec=25s\nRestart=on-failure\nRestartSec=10s\n\
[Install]\nWantedBy=multi-user.target\n"))
}

pub fn start_service(bundle: &Path, request: &str, replaces: Option<&str>) -> Result<()> {
    if !Path::new("/run/systemd/system").is_dir() {
        return Err("서비스를 시작하려면 systemd로 부팅한 Ubuntu가 필요합니다.".into());
    }
    let uid = unsafe { libc::getuid() };
    let user = current_user(uid)?;
    let bundle = bundle.canonicalize()?;
    let home = std::env::var("HOME")?;
    let cwd = std::env::current_dir()?;
    let kubeconfig = match std::env::var_os("KUBECONFIG") {
        Some(paths) => std::env::join_paths(
            std::env::split_paths(&paths)
                .filter(|p| !p.as_os_str().is_empty())
                .map(|p| cwd.join(p)),
        )?
        .into_string()
        .map_err(|_| "invalid kubeconfig path")?,
        None => String::new(),
    };
    let mut selectors = CLOUD_SELECTORS.map(|name| std::env::var(name).unwrap_or_default());
    if selectors[1].is_empty() {
        selectors[1] = std::env::var("AWS_DEFAULT_REGION").unwrap_or_default();
    }
    unit(&bundle, uid, request, &home, &kubeconfig, &selectors)?;
    let selectors = serde_json::to_string(&selectors)?;
    let (_, executable, digest) = signed_installer(&bundle, uid)?;
    let mut tty = OpenOptions::new()
        .read(true)
        .write(true)
        .open("/dev/tty")
        .map_err(|_| "서비스 설치 승인을 위해 대화형 터미널에서 실행해 주세요.")?;
    write!(tty, "\n{user} 사용자로 보안 연결 서비스를 설치하고 부팅 시 시작하도록 설정합니다. 승인하시겠습니까? [y/N]: ")?;
    tty.flush()?;
    let mut answer = String::new();
    BufReader::new(tty.try_clone()?)
        .take(257)
        .read_line(&mut answer)?;
    if !matches!(answer.trim(), "y" | "Y" | "yes" | "YES") {
        return Err("서비스 설치를 취소했습니다.".into());
    }
    approved_install(
        &executable,
        &digest,
        &[
            "install-service".as_ref(),
            bundle.as_os_str(),
            uid.to_string().as_ref(),
            user.as_ref(),
            request.as_ref(),
            home.as_ref(),
            kubeconfig.as_ref(),
            replaces.unwrap_or_default().as_ref(),
            selectors.as_ref(),
        ],
    )
}

/// Root only writes a unit for the invoking non-root user. User paths are never
/// executed by the system manager as root.
pub fn install_service(
    bundle: &Path,
    uid: &str,
    user: &str,
    request: &str,
    home: &str,
    kubeconfig: &str,
    replaces: Option<&str>,
    selectors: Option<&str>,
) -> Result<()> {
    if let Some(previous) = replaces {
        crate::api::identifier(previous)?;
    }
    let uid = uid.parse::<u32>()?;
    installation_account(uid, user)?;
    let selectors = selectors
        .map(serde_json::from_str::<[String; 3]>)
        .transpose()?
        .unwrap_or_default();
    let content = unit(bundle, uid, request, home, kubeconfig, &selectors)?;
    for path in ["/", "/etc", "/etc/systemd", SERVICE_DIR, "/run"] {
        trusted_directory(Path::new(path))?;
    }
    let _lock = install_lock("/run/migration-connector-service.lock")?;
    let path = Path::new(SERVICE_DIR).join(SERVICE);
    let old = existing(&path)?;
    let changed = old.as_deref() != Some(content.as_bytes());
    if let Some(old) = &old {
        let old = std::str::from_utf8(old)?;
        let header = format!("# migration-connector uid={uid} request=");
        let old_request = old
            .lines()
            .next()
            .and_then(|line| line.strip_prefix(&header))
            .ok_or("기존 서비스가 다른 사용자 또는 관리 설정에 속해 있습니다.")?;
        crate::api::identifier(old_request)?;
        if old_request != request {
            if replaces == Some(old_request) {
                run(SYSTEMCTL, &["stop", SERVICE])?;
            } else {
                let status = Command::new(SYSTEMCTL)
                    .args(["show", "--property=ActiveState", "--value", SERVICE])
                    .env_clear()
                    .output()?;
                if !status.status.success()
                    || !matches!(status.stdout.as_slice(), b"inactive\n" | b"failed\n")
                {
                    return Err("기존 연결 서비스를 중지한 후 새 연결을 설치해 주세요.".into());
                }
            }
        }
    }
    if changed {
        atomic(&path, content.as_bytes(), 0o644)?;
    }
    run(SYSTEMCTL, &["daemon-reload"])?;
    run(
        SYSTEMCTL,
        &[
            if changed && old.is_some() {
                "restart"
            } else {
                "start"
            },
            SERVICE,
        ],
    )?;
    run(SYSTEMCTL, &["is-active", "--quiet", SERVICE])?;
    run(SYSTEMCTL, &["enable", SERVICE])
}

fn trusted_directory(path: &Path) -> Result<()> {
    let m = fs::symlink_metadata(path)?;
    if !m.is_dir() || m.uid() != 0 || m.mode() & 0o022 != 0 {
        return Err("관리자 설치 경로의 소유권·권한을 확인하지 못했습니다.".into());
    }
    Ok(())
}
fn ensure_directory(path: &Path) -> Result<()> {
    if let Some(parent) = path.parent() {
        trusted_directory(parent)?;
    }
    match DirBuilder::new().mode(0o755).create(path) {
        Ok(()) => fs::set_permissions(path, fs::Permissions::from_mode(0o755))?,
        Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => (),
        Err(e) => return Err(e.into()),
    }
    trusted_directory(path)
}
fn existing(path: &Path) -> Result<Option<Vec<u8>>> {
    match fs::symlink_metadata(path) {
        Ok(_) => Ok(Some(read_regular(path, 0, MAX_ARTIFACT)?)),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(e.into()),
    }
}
fn atomic(path: &Path, b: &[u8], mode: u32) -> Result<()> {
    let parent = path.parent().ok_or("설치 경로")?;
    trusted_directory(parent)?;
    let tmp = parent.join(format!(
        ".migration-install-{}",
        crypto::b64(&crypto::random::<16>()?)
    ));
    create(&tmp, b, mode)?;
    if let Err(e) = fs::rename(&tmp, path) {
        let _ = fs::remove_file(&tmp);
        return Err(e.into());
    }
    File::open(parent)?.sync_all()?;
    Ok(())
}
fn run(bin: &str, args: &[&str]) -> Result<()> {
    if !Command::new(bin)
        .args(args)
        .env_clear()
        .env("PATH", "/usr/sbin:/usr/bin:/sbin:/bin")
        .env("DEBIAN_FRONTEND", "noninteractive")
        .stdin(Stdio::null())
        .status()?
        .success()
    {
        return Err("단말 패키지 설치 또는 관리자 설정 검사에 실패했습니다.".into());
    }
    Ok(())
}
fn install_lock(path: &str) -> Result<File> {
    let lock = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)?;
    let meta = lock.metadata()?;
    if !meta.is_file()
        || meta.uid() != 0
        || meta.mode() & 0o077 != 0
        || unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0
    {
        return Err("다른 네트워크 설정 작업이 진행 중입니다. 다시 시도해 주세요.".into());
    }
    Ok(lock)
}
fn missing_packages() -> Result<Vec<&'static str>> {
    let mut missing = Vec::new();
    for &package in PACKAGES {
        let o = Command::new("/usr/bin/dpkg-query")
            .args(["-W", "-f=${db:Status-Status}", "--", package])
            .env_clear()
            .output()?;
        if !o.status.success() || o.stdout != b"installed" {
            missing.push(package);
        }
    }
    Ok(missing)
}
/// Root entry point. Copies untrusted source bytes into root-only staging and re-verifies both signatures.
pub fn install(bundle: &Path, uid: u32, user: &str) -> Result<()> {
    if !cfg!(target_os = "linux") || unsafe { libc::geteuid() } != 0 {
        return Err("Linux 관리자 권한이 필요합니다.".into());
    }
    installation_account(uid, user)?;
    let os = fs::read_to_string("/etc/os-release")?;
    if !os.lines().any(|l| l == "ID=ubuntu") || !os.lines().any(|l| l == "VERSION_ID=\"24.04\"") {
        return Err("Ubuntu 24.04에서만 자동 설치할 수 있습니다.".into());
    }
    let stage = Stage::new()?;
    let raw = read_regular(&bundle.join("manifest.json"), uid, 65536)?;
    create(&stage.0.join("manifest.json"), &raw, 0o600)?;
    let v: Value = serde_json::from_slice(&raw)?;
    let artifacts: Vec<release::Artifact> =
        serde_json::from_value(v["payload"]["artifacts"].clone())?;
    if artifacts.is_empty() || artifacts.len() > 32 {
        return Err("설치 파일 수 제한".into());
    }
    let mut total = 0u64;
    for a in artifacts {
        total = total.checked_add(a.size).ok_or("설치 크기 제한")?;
        if !safe_name(&a.name)
            || a.name == "manifest.json"
            || a.size == 0
            || a.size > MAX_ARTIFACT
            || total > 512 * 1024 * 1024
        {
            return Err("설치 파일 목록이 올바르지 않습니다.".into());
        }
        create(
            &stage.0.join(&a.name),
            &read_regular(&bundle.join(&a.name), uid, a.size)?,
            0o600,
        )?;
    }
    let manifest = release::verify_bundle_artifacts(&stage.0)?;
    required(&manifest)?;
    let p = policy(
        &read_regular(&stage.0.join("net-policy.json"), 0, 65536)?,
        uid,
    )?;
    let helper = read_regular(&stage.0.join("migration-net-helper"), 0, MAX_ARTIFACT)?;
    let policy_bytes = serde_json::to_vec(&p)?;
    for path in ["/", "/usr", "/usr/local", "/etc"] {
        trusted_directory(Path::new(path))?;
    }
    for path in [
        "/usr/local/libexec",
        "/etc/migration-connector",
        "/etc/sudoers.d",
    ] {
        if fs::symlink_metadata(path).is_ok() {
            trusted_directory(Path::new(path))?;
        }
    }
    let old_policy = existing(Path::new(POLICY))?;
    if let Some(bytes) = &old_policy {
        let old: Policy = serde_json::from_slice(bytes)?;
        if serde_json::to_value(old)? != serde_json::to_value(&p)? {
            return Err(
                "기존 관리자 네트워크 정책과 다릅니다. 기존 정책을 자동 덮어쓰지 않습니다.".into(),
            );
        }
    }
    let old_helper = existing(Path::new(HELPER))?;
    let old_floor = existing(Path::new(FLOOR))?;
    let floor = match &old_floor {
        Some(b) => std::str::from_utf8(b)?.trim().parse::<u64>()?,
        None => 0,
    };
    if manifest.sequence < floor {
        return Err("이전에 설치한 배포보다 낮은 순번은 설치하지 않습니다.".into());
    }
    let sudo_path = PathBuf::from(format!("/etc/sudoers.d/migration-connector-{uid}"));
    let sudo_rule = format!("{user} ALL=(root) NOPASSWD: {HELPER} \"\"\n");
    let old_sudo = existing(&sudo_path)?;
    if let Some(bytes) = &old_sudo {
        if bytes.as_slice() != sudo_rule.as_bytes() {
            return Err("기존 관리자 권한 규칙과 충돌합니다.".into());
        }
    }
    // A signed helper can be atomically replaced under the network lock while
    // retaining the existing policy and the current interface.
    let existing_install = old_policy.is_some() && old_helper.is_some();
    if !existing_install && fs::symlink_metadata("/run/migration-connector-network.json").is_ok() {
        return Err("활성 연결의 기존 보조 프로그램과 정책을 확인할 수 없습니다.".into());
    }
    let sudo_stage = stage.0.join("sudoers");
    create(&sudo_stage, sudo_rule.as_bytes(), 0o440)?;
    run(
        "/usr/sbin/visudo",
        &["-cf", sudo_stage.to_str().ok_or("설치 경로")?],
    )?;
    let missing = missing_packages()?;
    if !missing.is_empty() {
        run("/usr/bin/apt-get", &["update"])?;
        let mut args = vec!["install", "-y", "--no-install-recommends"];
        args.extend(missing);
        run("/usr/bin/apt-get", &args)?;
    }
    // Package downloads can take minutes. Lock only the short configuration commit,
    // then recheck the snapshots so an intervening administrator edit is preserved.
    trusted_directory(Path::new("/run"))?;
    let _lock = install_lock("/run/migration-connector-network.lock")?;
    if (!existing_install && fs::symlink_metadata("/run/migration-connector-network.json").is_ok())
        || existing(Path::new(HELPER))? != old_helper
        || existing(Path::new(POLICY))? != old_policy
        || existing(Path::new(FLOOR))? != old_floor
        || existing(&sudo_path)? != old_sudo
    {
        return Err(
            "설치 중 기존 네트워크 상태나 관리자 설정이 변경되어 파일 설치를 중단했습니다.".into(),
        );
    }
    ensure_directory(Path::new("/usr/local/libexec"))?;
    ensure_directory(Path::new("/etc/migration-connector"))?;
    ensure_directory(Path::new("/etc/sudoers.d"))?;
    atomic(Path::new(HELPER), &helper, 0o755)?;
    atomic(Path::new(POLICY), &policy_bytes, 0o644)?;
    atomic(&sudo_path, sudo_rule.as_bytes(), 0o440)?;
    atomic(
        Path::new(FLOOR),
        manifest.sequence.to_string().as_bytes(),
        0o644,
    )?;
    Ok(())
}
#[cfg(test)]
mod tests {
    use super::*;
    fn p() -> Vec<u8> {
        br#"{"uid":0,"interface":"mc0","local_address":"10.240.0.2","peer_address":"10.240.0.1","endpoint":"192.0.2.10:51820","listen_port":51820,"job_port":18080,"probe_port":18081,"role":"client"}"#.to_vec()
    }
    #[test]
    fn only_client_policy_can_bind_current_user() {
        assert_eq!(policy(&p(), 1001).unwrap().uid, 1001);
        let mut v: Value = serde_json::from_slice(&p()).unwrap();
        v["uid"] = 1002.into();
        assert!(policy(&serde_json::to_vec(&v).unwrap(), 1001).is_err());
        v["uid"] = 0.into();
        v["role"] = "gateway".into();
        assert!(policy(&serde_json::to_vec(&v).unwrap(), 1001).is_err());
        v["role"] = "client".into();
        v["interface"] = "eth0".into();
        assert!(policy(&serde_json::to_vec(&v).unwrap(), 1001).is_err());
    }
    #[test]
    fn unsigned_extra_files_cannot_authorize_install() {
        let mut m = release::Manifest {
            version: 1,
            sequence: 1,
            expires_at_ms: 100,
            artifacts: vec![],
        };
        assert!(required(&m).is_err());
        for n in [
            "migration-connector",
            "migration-net-helper",
            "net-policy.json",
        ] {
            m.artifacts.push(release::Artifact {
                name: n.into(),
                sha384: String::new(),
                size: 1,
            });
        }
        assert!(required(&m).is_ok());
        m.artifacts.pop();
        assert!(required(&m).is_err());
    }
    #[test]
    fn file_names_cannot_escape_staging() {
        for n in [
            "",
            ".",
            "..",
            "../etc/shadow",
            "/etc/passwd",
            "a/b",
            "a\\b",
            "-x",
        ] {
            assert!(!safe_name(n));
        }
        assert!(safe_name("migration-net-helper"));
        assert!(safe_name("net-policy.json"));
    }
    #[test]
    fn service_unit_binds_the_request_and_quotes_local_paths() {
        let content = unit(
            Path::new(r#"/home/client/with spaces/%u "quoted""#),
            1001,
            "request_12345678901234",
            "/home/client",
            "/home/client/a config:/home/client/$KUBE",
            &["demo-profile".into(), "ap-northeast-2".into(), "demo-config".into()],
        )
        .unwrap();
        assert!(content.contains(r#""AWS_PROFILE=demo-profile" "AWS_REGION=ap-northeast-2" "CLOUDSDK_ACTIVE_CONFIG_NAME=demo-config""#));
        assert!(content.contains("Type=notify\nNotifyAccess=main\nUser=1001\n"));
        assert!(content.contains(
            r#"ExecStart=:/bin/sh -c "exec \"$@\"" migration-connector-launch "/home/client/with spaces/%%u \"quoted\"/migration-connector" serve "#
        ));
        assert!(content.contains(r#"ExecCondition=:/usr/bin/test -f "/home/client/.local/state/migration-connector/request_12345678901234/state.json""#));
        assert!(unit(
            Path::new("/bundle"),
            0,
            "request_12345678901234",
            "/root",
            "",
            &Default::default()
        )
        .is_err());
        assert!(unit(
            Path::new("/bundle"),
            1001,
            "request_12345678901234",
            "/home/client\n[Service]",
            "",
            &Default::default()
        )
        .is_err());
    }
    #[test]
    fn root_launcher_checks_actual_copy_before_execution() {
        assert!(
            STAGE_SCRIPT.find("sha384sum --check --status").unwrap()
                < STAGE_SCRIPT.find("install-host").unwrap()
        );
        assert!(STAGE_SCRIPT.contains("/usr/bin/install -m 0700 -- \"$1\""));
    }
}
