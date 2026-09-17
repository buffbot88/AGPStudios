use std::process::Stdio;

use tokio::{
    io::AsyncWriteExt,
    process::Command,
};

#[derive(Clone)]
pub(crate) struct MailConfig {
    pub(crate) sendmail_path: String,
    pub(crate) from: String,
    pub(crate) public_url: Option<String>,
}

impl MailConfig {
    pub(crate) fn from_env(public_url: Option<&str>) -> Self {
        Self {
            sendmail_path: std::env::var("ASHAT_SENDMAIL_PATH")
                .unwrap_or_else(|_| "/usr/sbin/sendmail".to_owned()),
            from: std::env::var("ASHAT_MAIL_FROM")
                .unwrap_or_else(|_| "admin@agpstudios.org".to_owned()),
            public_url: public_url
                .map(|value| value.trim_end_matches('/'))
                .filter(|value| !value.is_empty())
                .map(str::to_owned),
        }
    }

    pub(crate) fn verification_url(&self, token: &str) -> Option<String> {
        let base = self.public_url.as_deref()?.trim_end_matches('/');
        Some(format!(
            "{base}/auth/verify-email?token={}",
            urlencoding::encode(token)
        ))
    }

    pub(crate) fn password_reset_url(&self, token: &str) -> Option<String> {
        let base = self.public_url.as_deref()?.trim_end_matches('/');
        Some(format!(
            "{base}/auth/reset-password?token={}",
            urlencoding::encode(token)
        ))
    }
}

pub(crate) async fn send_text(
    config: &MailConfig,
    to: &str,
    subject: &str,
    body: &str,
) -> Result<(), String> {
    if !valid_header_value(&config.from)
        || !valid_header_value(to)
        || !valid_header_value(subject)
        || body.len() > 64 * 1024
    {
        return Err("invalid email message".to_owned());
    }

    let message = format!(
        "From: {}\nTo: {}\nSubject: {}\nMIME-Version: 1.0\nContent-Type: text/plain; charset=UTF-8\nContent-Transfer-Encoding: 8bit\n\n{}\n",
        config.from,
        to,
        subject,
        body.replace('\r', ""),
    );

    let mut child = Command::new(&config.sendmail_path)
        .args(["-t", "-i", "-f", &config.from])
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|error| format!("unable to start mail transport: {error}"))?;

    if let Some(mut stdin) = child.stdin.take() {
        stdin
            .write_all(message.as_bytes())
            .await
            .map_err(|error| format!("unable to write email: {error}"))?;
    }

    let output = child
        .wait_with_output()
        .await
        .map_err(|error| format!("mail transport failed: {error}"))?;
    if output.status.success() {
        Ok(())
    } else {
        Err(format!(
            "mail transport exited with status {}",
            output.status
        ))
    }
}

fn valid_header_value(value: &str) -> bool {
    !value.trim().is_empty() && !value.contains('\r') && !value.contains('\n')
}

#[cfg(test)]
mod tests {
    use super::{valid_header_value, MailConfig};

    #[test]
    fn mail_urls_use_the_configured_public_origin() {
        let config = MailConfig {
            sendmail_path: "/usr/sbin/sendmail".to_owned(),
            from: "admin@agpstudios.org".to_owned(),
            public_url: Some("https://agpstudios.org/".to_owned()),
        };
        assert_eq!(
            config.verification_url("abc"),
            Some("https://agpstudios.org/auth/verify-email?token=abc".to_owned())
        );
        assert_eq!(
            config.password_reset_url("abc"),
            Some("https://agpstudios.org/auth/reset-password?token=abc".to_owned())
        );
    }

    #[test]
    fn mail_headers_reject_injection() {
        assert!(valid_header_value("person@example.test"));
        assert!(!valid_header_value("person@example.test\nBcc: bad@example.test"));
        assert!(!valid_header_value("  "));
    }
}
