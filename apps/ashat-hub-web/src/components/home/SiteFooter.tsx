import { FOOTER } from '../../content';

function SocialIcon({ icon }: { icon: string }) {
  switch (icon) {
    case 'discord':
      return (
        <svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor" aria-hidden>
          <path d="M19.3 5.3A16.6 16.6 0 0 0 15.1 4l-.2.4c1.5.4 2.9 1 4.2 1.9A13.4 13.4 0 0 0 7 6.3c1.3-.9 2.7-1.5 4.2-1.9L11 4a16.6 16.6 0 0 0-4.2 1.3C4.3 8.9 3.6 12.4 4 15.9A16.8 16.8 0 0 0 9 18.4l.9-1.5c-.8-.3-1.6-.7-2.3-1.2l.5-.4a11.9 11.9 0 0 0 10 0l.5.4c-.7.5-1.5.9-2.3 1.2l.9 1.5a16.7 16.7 0 0 0 5-2.5c.4-4-.7-7.5-2.9-10.6ZM9.7 13.9c-.9 0-1.7-.8-1.7-1.9s.7-1.9 1.7-1.9 1.7.9 1.7 1.9-.8 1.9-1.7 1.9Zm5.8 0c-.9 0-1.7-.8-1.7-1.9s.7-1.9 1.7-1.9 1.7.9 1.7 1.9-.7 1.9-1.7 1.9Z" />
        </svg>
      );
    case 'youtube':
      return (
        <svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor" aria-hidden>
          <path d="M21.6 7.2a2.5 2.5 0 0 0-1.8-1.8C18.2 5 12 5 12 5s-6.2 0-7.8.4A2.5 2.5 0 0 0 2.4 7.2 26 26 0 0 0 2 12a26 26 0 0 0 .4 4.8 2.5 2.5 0 0 0 1.8 1.8c1.6.4 7.8.4 7.8.4s6.2 0 7.8-.4a2.5 2.5 0 0 0 1.8-1.8A26 26 0 0 0 22 12a26 26 0 0 0-.4-4.8ZM10 15.2V8.8l5.2 3.2L10 15.2Z" />
        </svg>
      );
    default:
      return null;
  }
}

export function SiteFooter() {
  return (
    <footer className="site-footer">
      <div className="footer-inner">
        <a className="wordmark" href="#top">
          <b>AGP</b> STUDIOS
        </a>
        <span className="footer-legal-inline">© 2026 AGP Studios</span>
        <div className="footer-group">
          <span className="footer-identity">{FOOTER.tagline}</span>
          <div className="footer-socials">
            {FOOTER.socials.map((social) => (
              <a key={social.icon} href={social.href} aria-label={social.label} target="_blank" rel="noreferrer">
                <SocialIcon icon={social.icon} />
              </a>
            ))}
          </div>
        </div>
      </div>
    </footer>
  );
}
