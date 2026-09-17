-- Seed test releases for Vesper Studios
-- Platforms: windows-x86_64, linux-x86_64, darwin-aarch64

INSERT IGNORE INTO vesper_releases (id, version, platform_rid, pub_date, notes, filename, signature, file_size, download_url, is_latest) VALUES
-- v1.0.0 — initial release
('vr-001', '1.0.0', 'windows-x86_64', '2026-07-01 12:00:00',
 'Initial Vesper Studios release.\n- Project manager with file tree\n- Built-in code editor\n- Assistant panel\n- Live Vite preview\n- Deployment controls',
 'vesper-1.0.0-x64-setup.exe', 'sig_win_100', 52428800, '', 0),
('vr-002', '1.0.0', 'linux-x86_64', '2026-07-01 12:00:00',
 'Initial Vesper Studios release.\n- Project manager with file tree\n- Built-in code editor\n- Assistant panel\n- Live Vite preview\n- Deployment controls',
 'vesper-1.0.0-x86_64.AppImage', 'sig_linux_100', 48234496, '', 0),
('vr-003', '1.0.0', 'darwin-aarch64', '2026-07-01 12:00:00',
 'Initial Vesper Studios release.\n- Project manager with file tree\n- Built-in code editor\n- Assistant panel\n- Live Vite preview\n- Deployment controls',
 'vesper-1.0.0-aarch64.dmg', 'sig_mac_100', 50331648, '', 0),

-- v1.1.0 — assistant improvements
('vr-004', '1.1.0', 'windows-x86_64', '2026-07-20 10:00:00',
 'Assistant improvements:\n- Streaming responses\n- Context-aware suggestions\n- File diff preview before applying changes\n- Bug fixes for Vite preview reload',
 'vesper-1.1.0-x64-setup.exe', 'sig_win_110', 54067200, '', 0),
('vr-005', '1.1.0', 'linux-x86_64', '2026-07-20 10:00:00',
 'Assistant improvements:\n- Streaming responses\n- Context-aware suggestions\n- File diff preview before applying changes\n- Bug fixes for Vite preview reload',
 'vesper-1.1.0-x86_64.AppImage', 'sig_linux_110', 49807360, '', 0),
('vr-006', '1.1.0', 'darwin-aarch64', '2026-07-20 10:00:00',
 'Assistant improvements:\n- Streaming responses\n- Context-aware suggestions\n- File diff preview before applying changes\n- Bug fixes for Vite preview reload',
 'vesper-1.1.0-aarch64.dmg', 'sig_mac_110', 51904512, '', 0),

-- v1.2.0 — deployment & multi-file editing
('vr-007', '1.2.0', 'windows-x86_64', '2026-08-10 14:00:00',
 'Deployment & editing upgrades:\n- One-click deploy to Vercel/Netlify\n- Multi-file tab editing\n- Terminal panel with full shell access\n- Project import from GitHub\n- Improved startup time',
 'vesper-1.2.0-x64-setup.exe', 'sig_win_120', 57671680, '', 0),
('vr-008', '1.2.0', 'linux-x86_64', '2026-08-10 14:00:00',
 'Deployment & editing upgrades:\n- One-click deploy to Vercel/Netlify\n- Multi-file tab editing\n- Terminal panel with full shell access\n- Project import from GitHub\n- Improved startup time',
 'vesper-1.2.0-x86_64.AppImage', 'sig_linux_120', 53479424, '', 0),
('vr-009', '1.2.0', 'darwin-aarch64', '2026-08-10 14:00:00',
 'Deployment & editing upgrades:\n- One-click deploy to Vercel/Netlify\n- Multi-file tab editing\n- Terminal panel with full shell access\n- Project import from GitHub\n- Improved startup time',
 'vesper-1.2.0-aarch64.dmg', 'sig_mac_120', 55576576, '', 0),

-- v1.3.0 — latest (current)
('vr-010', '1.3.0', 'windows-x86_64', '2026-08-16 09:00:00',
 'August update:\n- V8 theme with clean dark UI\n- Google account sign-in\n- File search across project\n- Git integration (commit, branch, diff)\n- Performance improvements\n- Bug fixes',
 'vesper-1.3.0-x64-setup.exe', 'sig_win_130', 60817408, '', 1),
('vr-011', '1.3.0', 'linux-x86_64', '2026-08-16 09:00:00',
 'August update:\n- V8 theme with clean dark UI\n- Google account sign-in\n- File search across project\n- Git integration (commit, branch, diff)\n- Performance improvements\n- Bug fixes',
 'vesper-1.3.0-x86_64.AppImage', 'sig_linux_130', 56625152, '', 1),
('vr-012', '1.3.0', 'darwin-aarch64', '2026-08-16 09:00:00',
 'August update:\n- V8 theme with clean dark UI\n- Google account sign-in\n- File search across project\n- Git integration (commit, branch, diff)\n- Performance improvements\n- Bug fixes',
 'vesper-1.3.0-aarch64.dmg', 'sig_mac_130', 58722304, '', 1);
