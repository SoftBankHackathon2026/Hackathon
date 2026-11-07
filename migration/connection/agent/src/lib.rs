pub mod crypto;
pub mod network;
pub mod protocol;
pub mod release;
pub mod transport;
pub type Result<T> = std::result::Result<T, Box<dyn std::error::Error + Send + Sync>>;
pub fn now_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .expect("clock")
        .as_millis() as u64
}

pub mod api;

pub mod host_install;
