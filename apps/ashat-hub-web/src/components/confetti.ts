/**
 * Celebration confetti: a one-shot canvas burst inside a stat card.
 *
 * Hand-rolled on purpose — no dependency for ~60 lines. Particles launch from
 * the card center with site-palette colors, fall with gravity, and the canvas
 * removes itself when the animation ends. Honors prefers-reduced-motion.
 */
export function burstConfetti(host: HTMLElement): void {
  try {
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return;
  } catch {
    return;
  }
  const rect = host.getBoundingClientRect();
  if (rect.width < 2 || rect.height < 2) return;
  const canvas = document.createElement('canvas');
  canvas.className = 'confetti-burst';
  canvas.setAttribute('aria-hidden', 'true');
  canvas.width = Math.floor(rect.width);
  canvas.height = Math.floor(rect.height);
  host.appendChild(canvas);
  const ctx = canvas.getContext('2d');
  if (!ctx) {
    canvas.remove();
    return;
  }
  const colors = ['#f1c25b', '#7fd4c1', '#e8e4da', '#dd6159', '#8fb8d8'];
  type Particle = {
    x: number; y: number; vx: number; vy: number;
    size: number; color: string; rot: number; vr: number;
  };
  const parts: Particle[] = Array.from({ length: 90 }, () => ({
    x: canvas.width / 2,
    y: canvas.height / 2,
    vx: (Math.random() - 0.5) * 9,
    vy: Math.random() * -7 - 2,
    size: 3 + Math.random() * 4,
    color: colors[(Math.random() * colors.length) | 0],
    rot: Math.random() * Math.PI,
    vr: (Math.random() - 0.5) * 0.3,
  }));
  const start = performance.now();
  const tick = (now: number) => {
    const elapsed = (now - start) / 1600;
    if (elapsed >= 1 || !canvas.isConnected) {
      canvas.remove();
      return;
    }
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    for (const part of parts) {
      part.x += part.vx;
      part.y += part.vy;
      part.vy += 0.25;
      part.rot += part.vr;
      ctx.save();
      ctx.globalAlpha = 1 - elapsed;
      ctx.translate(part.x, part.y);
      ctx.rotate(part.rot);
      ctx.fillStyle = part.color;
      ctx.fillRect(-part.size / 2, -part.size / 4, part.size, part.size / 2);
      ctx.restore();
    }
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}
