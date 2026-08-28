import { useEffect, useState, type ReactNode } from 'react';

export function AuthGate({ children }: { children: ReactNode }) {
  const [allowed, setAllowed] = useState(false);
  useEffect(() => {
    fetch('/api/auth/session', { credentials: 'include' })
      .then((response) => response.json())
      .then((data: { authenticated?: boolean }) => {
        if (data.authenticated) setAllowed(true);
        else window.location.href = 'https://agpstudios.org/?return=/galileo/';
      })
      .catch(() => { window.location.href = 'https://agpstudios.org/?return=/galileo/'; });
  }, []);
  return allowed ? children : <div className="h-full w-full" aria-busy="true" />;
}
