use crate::Result;
use std::sync::atomic::{AtomicBool, Ordering};

static STOP: AtomicBool = AtomicBool::new(false);

#[cfg(target_os = "linux")]
extern "C" fn stop(_: libc::c_int) {
    STOP.store(true, Ordering::Relaxed);
}

#[cfg(target_os = "linux")]
pub fn install_stop_handler() -> Result<()> {
    unsafe {
        let mut action: libc::sigaction = std::mem::zeroed();
        action.sa_sigaction = stop as *const () as usize;
        libc::sigemptyset(&mut action.sa_mask);
        if libc::sigaction(libc::SIGTERM, &action, std::ptr::null_mut()) != 0 {
            return Err(std::io::Error::last_os_error().into());
        }
    }
    Ok(())
}

#[cfg(not(target_os = "linux"))]
pub fn install_stop_handler() -> Result<()> {
    Err("Linux systemd required".into())
}

pub fn stopping() -> bool {
    STOP.load(Ordering::Relaxed)
}

#[cfg(target_os = "linux")]
pub fn ready() -> Result<()> {
    use std::os::{
        linux::net::SocketAddrExt,
        unix::{
            ffi::OsStrExt,
            net::{SocketAddr, UnixDatagram},
        },
    };
    let socket = std::env::var_os("NOTIFY_SOCKET").ok_or("missing systemd notification socket")?;
    let address = match socket.as_bytes().strip_prefix(b"@") {
        Some(name) => SocketAddr::from_abstract_name(name)?,
        None => SocketAddr::from_pathname(socket)?,
    };
    UnixDatagram::unbound()?.send_to_addr(b"READY=1", &address)?;
    Ok(())
}

#[cfg(not(target_os = "linux"))]
pub fn ready() -> Result<()> {
    Err("Linux systemd required".into())
}
