import Phaser from 'phaser';
import type { LaneKey, LaneStatus, TelemetryFrame, TelemetrySnapshot } from './types';

const COLORS = {
  ink: 0x0d0d0f,
  bgSoft: 0x121215,
  panel: 0x17171b,
  panelRaised: 0x1f1f25,
  line: 0x2a2a31,
  lineSoft: 0x222228,
  text: 0xe9e9ee,
  textSoft: 0xb3b3bd,
  muted: 0x8f8f9a,
  dim: 0x5c5c66,
  accent: 0xff7a45,
  accentHover: 0xff9468,
  green: 0x47d48f,
  amber: 0xf2b23e,
  red: 0xff6b6b,
};

const FONT = 'Inter, ui-sans-serif, system-ui, sans-serif';
const HEADING = 'Newsreader, Georgia, serif';
const MONO = 'JetBrains Mono, ui-monospace, monospace';

const LANE_ORDER: LaneKey[] = ['omega', 'beta', 'delta'];

const LANE_URLS: Record<LaneKey, string> = {
  omega: '',
  beta: 'https://ashatneuralhost2.agpstudios.org',
  delta: 'https://ashatneuralhost3.agpstudios.org',
};

export class TelemetryScene extends Phaser.Scene {
  private snapshot: TelemetrySnapshot;
  private root!: Phaser.GameObjects.Container;
  private canvasWidth = 0;
  private canvasHeight = 0;
  private connectionLabel!: Phaser.GameObjects.Text;
  private updatedLabel!: Phaser.GameObjects.Text;
  private statusDot!: Phaser.GameObjects.Arc;
  private resizeHandler?: () => void;
  private pulseTween?: Phaser.Tweens.Tween;
  private selectedChartLane: LaneKey = 'omega';
  private modelTickerText!: Phaser.GameObjects.Text;
  private modelTickerTimer?: Phaser.Time.TimerEvent;
  private modelIndex = 0;

  public constructor(initialSnapshot: TelemetrySnapshot) {
    super({ key: 'TelemetryScene' });
    this.snapshot = initialSnapshot;
  }

  public preload(): void {
    this.load.image('ashat-logo-large', '/images/lion-logo-128.png');
    this.load.image('ashat-logo-small', '/images/lion-logo-32.png');
  }

  public create(): void {
    this.canvasWidth = this.scale.width;
    this.canvasHeight = this.scale.height;
    this.draw();
    this.resizeHandler = () => {
      this.canvasWidth = this.scale.width;
      this.canvasHeight = this.scale.height;
      this.draw();
    };
    this.scale.on(Phaser.Scale.Events.RESIZE, this.resizeHandler);
  }

  public shutdown(): void {
    if (this.resizeHandler) this.scale.off(Phaser.Scale.Events.RESIZE, this.resizeHandler);
    this.pulseTween?.stop();
    this.modelTickerTimer?.remove(false);
  }

  public setSnapshot(snapshot: TelemetrySnapshot): void {
    this.snapshot = snapshot;
    if (this.scene.isActive()) this.draw();
  }

  private draw(): void {
    this.pulseTween?.stop();
    this.modelTickerTimer?.remove(false);
    this.modelTickerTimer = undefined;
    this.root?.destroy(true);
    this.root = this.add.container(0, 0);

    const width = this.canvasWidth;
    const height = this.canvasHeight;
    this.drawBackdrop(width, height);

    const margin = Math.max(24, Math.min(64, width * 0.055));
    const compact = width < 850;
    const contentWidth = width - margin * 2;
    let y = compact ? 28 : 36;

    this.drawHeader(margin, y, contentWidth);
    y += compact ? 82 : 96;
    this.drawHero(margin, y, contentWidth);
    y += compact ? 124 : 144;

    const laneGap = 12;
    const cardWidth = (contentWidth - laneGap * 2) / 3;
    LANE_ORDER.forEach((key, index) => {
      const lane = this.snapshot.status.lanes[key];
      const cardX = margin + index * (cardWidth + laneGap);
      this.drawCompactLaneCard(cardX, y, cardWidth, lane, key);
    });
    y += compact ? 200 : 220;

    const chartFrames = this.snapshot.timeseries[this.selectedChartLane] ?? [];
    this.drawPerformanceChart(margin, y, contentWidth, this.selectedChartLane, chartFrames);

    this.drawFooter(width, height);
  }

  private drawBackdrop(width: number, height: number): void {
    const background = this.add.graphics();
    background.fillStyle(COLORS.ink, 1);
    background.fillRect(0, 0, width, height);
    this.root.add(background);
  }

  private drawHeader(x: number, y: number, width: number): void {
    const navLine = this.add.graphics();
    navLine.lineStyle(1, COLORS.line, 0.9);
    navLine.lineBetween(x, y + 48, x + width, y + 48);
    this.root.add(navLine);

    const homeLink = this.text('\u2190  BACK TO HOME', x + width / 2, y + 2, 9, COLORS.muted, false, MONO, 0.8).setOrigin(0.5, 0);
    homeLink.setInteractive({ cursor: 'pointer' });
    homeLink.on('pointerover', () => homeLink.setColor(`#${COLORS.accent.toString(16).padStart(6, '0')}`));
    homeLink.on('pointerout', () => homeLink.setColor(`#${COLORS.muted.toString(16).padStart(6, '0')}`));
    homeLink.on('pointerdown', () => window.open('https://www.agpstudios.org', '_blank'));

    const logo = this.add.image(x + 13, y + 14, 'ashat-logo-small').setDisplaySize(26, 26);
    this.root.add(logo);
    this.text('OMEGA', x + 38, y, 20, COLORS.text, true);
    this.text(' / NEURAL HOST', x + 118, y + 1, 16, COLORS.accent, false, HEADING);
    this.text('V 0.1  ·  ASHAT NEURAL HOST · MASTER EDITION', x, y + 29, 9, COLORS.muted, false, MONO, 1.5);

    const rightX = x + width;
    this.statusDot = this.add.circle(rightX - 148, y + 13, 5, this.snapshot.status.all_ready ? COLORS.green : COLORS.amber);
    this.root.add(this.statusDot);
    this.connectionLabel = this.text(
      this.snapshot.demo ? 'DEMO TELEMETRY' : 'LIVE TELEMETRY',
      rightX - 136,
      y + 5,
      11,
      this.snapshot.demo ? COLORS.amber : COLORS.green,
      true,
      MONO,
      0.8,
    );
    const lastRequestAt = new Date(
      this.snapshot.status.lanes.omega.last_request_time ?? new Date(this.snapshot.updatedAt),
    );
    this.updatedLabel = this.text(`LAST REQUEST  ${this.formatClock(lastRequestAt)}`, rightX - 148, y + 28, 9, COLORS.muted, false, MONO, 1);
    this.pulseTween = this.tweens.add({ targets: this.statusDot, alpha: 0.42, duration: 1300, yoyo: true, repeat: -1 });
  }

  private drawHero(x: number, y: number, width: number): void {
    const logo = this.add.image(x + 42, y + 42, 'ashat-logo-large').setDisplaySize(68, 68).setAlpha(0.92);
    this.root.add(logo);
    this.text('PUBLIC TELEMETRY', x + 86, y, 11, COLORS.accent, true, MONO, 2);
    this.text('Omega', x + 86, y + 22, Math.min(36, width * 0.055), COLORS.text, true, HEADING);
    const uptime = this.formatUptime(this.snapshot.status.uptime_seconds);
    this.text(`UPTIME  ${uptime}   ·   ORCHESTRATOR  ${this.snapshot.status.orchestrator_pool.baseline_alive ? 'OK' : 'DOWN'}`,
      x + width, y + 11, 10, COLORS.dim, false, MONO, 1.1).setOrigin(1, 0);
    this.drawModelCard(x + 86, y + 64, x + width);
  }

  /**
   * Model card: cycles through the active lanes' models (basename only —
   * never a filesystem path) and shows how many of the three lanes are in
   * use right now.
   */
  private drawModelCard(left: number, y: number, right: number): void {
    // One predicate for both the cycling list and the count: any lane that is
    // not `offline` is in use (online/busy/waking/degraded).
    const inUse = LANE_ORDER.filter((key) => this.snapshot.status.lanes[key].lane_state !== 'offline');
    const active = inUse.filter((key) => {
      const lane = this.snapshot.status.lanes[key];
      return lane.model && lane.model !== '—';
    });
    const lanesInUse = inUse.length;

    this.text('ACTIVE MODELS', left, y, 9, COLORS.muted, true, MONO, 1);
    this.modelTickerText = this.text('', left, y + 18, 12, COLORS.text, false, MONO);
    this.text(`LANES IN USE  ${lanesInUse}/${LANE_ORDER.length}`, right, y + 14, 10, COLORS.green, true, MONO, 1.2).setOrigin(1, 0);

    const cycle = () => {
      if (active.length === 0) {
        this.modelTickerText.setText('—');
        return;
      }
      const key = active[this.modelIndex % active.length];
      this.modelIndex += 1;
      const lane = this.snapshot.status.lanes[key];
      this.modelTickerText.setText(`${this.friendlyModel(lane.model)}  ·  ${key.toUpperCase()}`);
    };
    cycle();
    this.modelTickerTimer = this.time.addEvent({ delay: 3000, loop: true, callback: cycle });
  }

  /** Model basename for display: strip any directory and model-file
   *  extension so telemetry never shows the on-disk location. */
  private friendlyModel(raw: string): string {
    const base = raw.split('/').pop() ?? raw;
    return base.replace(/\.(gguf|bin)$/i, '').replace('-Q8_0', ' · Q8');
  }

  private drawCompactLaneCard(x: number, y: number, width: number, lane: LaneStatus, key: LaneKey): void {
    const cardHeight = this.canvasHeight < 600 ? 180 : 200;
    this.panel(x, y, width, cardHeight, COLORS.panel);

    const isOffline = lane.lane_state === 'offline';
    const stateColor = lane.lane_state === 'online' ? COLORS.green : lane.lane_state === 'degraded' ? COLORS.red : isOffline ? COLORS.dim : COLORS.amber;

    const number = (LANE_ORDER.indexOf(key) + 1).toString().padStart(2, '0');
    this.text(`${number}  /  ${lane.label.toUpperCase()}`, x + 16, y + 16, 9, COLORS.accent, true, MONO, 1.2);

    const pillW = 72;
    const pill = this.add.graphics();
    pill.fillStyle(stateColor, 0.12);
    pill.fillRoundedRect(x + width - pillW - 16, y + 12, pillW, 22, 11);
    pill.lineStyle(1, stateColor, 0.42);
    pill.strokeRoundedRect(x + width - pillW - 16, y + 12, pillW, 22, 11);
    this.root.add(pill);
    this.root.add(this.add.circle(x + width - pillW - 16 + 12, y + 23, 3, stateColor));
    this.text(lane.lane_state.toUpperCase(), x + width - pillW - 16 + 20, y + 17, 8, stateColor, true, MONO, 0.6);

    if (isOffline) {
      this.text('NO CONNECTION', x + 16, y + 48, 11, COLORS.dim, false, MONO, 0.8);
      const url = LANE_URLS[key];
      if (url) {
        this.text(url.replace('https://', ''), x + 16, y + 68, 8, COLORS.dim, false, MONO, 0.3);
      }
    } else {
      this.text(this.friendlyModel(lane.model), x + 16, y + 48, 10, COLORS.text, false, MONO, 0.1, width - 32);
    }

    this.rule(x + 16, y + 90, x + width - 16, COLORS.line);

    if (!isOffline) {
      const metricsCol1 = [
        ['SPEED', `${lane.avg_generation_tokens_per_second.toFixed(1)} t/s`],
        ['TOKENS', `${this.formatNumber(lane.total_prompt_tokens + lane.total_completion_tokens)}`],
      ];
      const metricsCol2 = [
        ['LATENCY', `${lane.avg_time_to_first_token_ms?.toFixed(0) ?? '—'}ms`],
        ['REQS', `${lane.total_requests.toLocaleString()}`],
      ];

      const colW = (width - 32) / 2;
      metricsCol1.forEach(([label, value], i) => {
        this.text(label, x + 16, y + 102 + i * 36, 8, COLORS.dim, true, MONO, 0.8);
        this.text(value, x + 16, y + 114 + i * 36, 13, i === 0 ? COLORS.accent : COLORS.text, true, MONO);
      });
      metricsCol2.forEach(([label, value], i) => {
        this.text(label, x + 16 + colW, y + 102 + i * 36, 8, COLORS.dim, true, MONO, 0.8);
        this.text(value, x + 16 + colW, y + 114 + i * 36, 13, COLORS.text, true, MONO);
      });

      this.text(`${lane.success_rate.toFixed(1)}% SUCCESS`, x + 16, y + cardHeight - 24, 8, COLORS.muted, false, MONO, 0.6);
    } else {
      this.text('STANDBY', x + 16, y + 110, 16, COLORS.dim, true, MONO, 1);
    }
  }

  private laneColor(key: LaneKey): number {
    if (key === 'beta') return COLORS.green;
    if (key === 'delta') return COLORS.amber;
    return COLORS.accent;
  }

  private drawPerformanceChart(x: number, y: number, width: number, laneKey: LaneKey, frames: TelemetryFrame[]): void {
    const height = 206;
    const laneColor = this.laneColor(laneKey);
    this.panel(x, y, width, height, COLORS.panel);
    this.text(`GENERATION VELOCITY  ·  ${laneKey.toUpperCase()}`, x + 20, y + 20, 10, laneColor, true, MONO, 1.5);

    // Lane selector chips (right-aligned: OMEGA | BETA | DELTA).
    const chipY = y + 14;
    const chipH = 22;
    let chipX = x + width - 20;
    [...LANE_ORDER].reverse().forEach((key) => {
      const label = key.toUpperCase();
      const chipW = label.length * 7.4 + 22;
      const cx = chipX - chipW;
      const selected = key === laneKey;
      const shape = this.add.graphics();
      shape.fillStyle(selected ? laneColor : COLORS.bgSoft, 1);
      shape.fillRoundedRect(cx, chipY, chipW, chipH, 11);
      shape.lineStyle(1, selected ? laneColor : COLORS.line, 1);
      shape.strokeRoundedRect(cx, chipY, chipW, chipH, 11);
      this.root.add(shape);
      const chipLabel = this.text(label, cx + chipW / 2, chipY + chipH / 2, 8, selected ? COLORS.ink : COLORS.muted, true, MONO, 1).setOrigin(0.5);
      chipLabel.setInteractive({ cursor: 'pointer' });
      chipLabel.on('pointerdown', () => {
        if (this.selectedChartLane !== key) {
          this.selectedChartLane = key;
          this.draw();
        }
      });
      chipLabel.on('pointerover', () => {
        if (!selected) chipLabel.setColor(`#${this.laneColor(key).toString(16).padStart(6, '0')}`);
      });
      chipLabel.on('pointerout', () => {
        if (!selected) chipLabel.setColor(`#${COLORS.muted.toString(16).padStart(6, '0')}`);
      });
      chipX = cx - 8;
    });

    const values = frames.map((frame) => frame.generation_tokens_per_second).filter((value): value is number => value !== null && value > 0);
    const current = values.at(-1) ?? 0;
    this.text(`${current.toFixed(1)}`, x + width - 20, y + 58, 27, COLORS.text, true, MONO).setOrigin(1, 0);
    this.text('TOKENS / SEC', x + width - 20, y + 92, 9, COLORS.muted, false, MONO, 1).setOrigin(1, 0);

    const chartX = x + 20;
    const chartY = y + 116;
    const chartWidth = width - 40;
    const chartHeight = 60;
    const graph = this.add.graphics();
    graph.lineStyle(1, COLORS.line, 0.8);
    for (let row = 0; row < 4; row += 1) graph.lineBetween(chartX, chartY + row * (chartHeight / 3), chartX + chartWidth, chartY + row * (chartHeight / 3));
    if (values.length > 0) {
      const min = Math.max(0, Math.min(...values) - 2);
      const max = Math.max(...values) + 2;
      const points = values.map((value, index) => ({
        x: chartX + (index / Math.max(values.length - 1, 1)) * chartWidth,
        y: chartY + chartHeight - ((value - min) / Math.max(max - min, 1)) * chartHeight,
      }));
      graph.lineStyle(2, laneColor, 1);
      points.forEach((point, index) => {
        if (index > 0) graph.lineBetween(points[index - 1].x, points[index - 1].y, point.x, point.y);
      });
      const last = points.at(-1);
      if (last) {
        graph.fillStyle(laneColor, 0.18);
        graph.fillCircle(last.x, last.y, 9);
        graph.fillStyle(laneColor, 1);
        graph.fillCircle(last.x, last.y, 3.5);
      }
    } else {
      this.text('NO DATA YET', chartX + chartWidth / 2, chartY + chartHeight / 2, 10, COLORS.dim, true, MONO, 1).setOrigin(0.5);
    }
    this.root.add(graph);
    this.text('− 30 MIN', chartX, chartY + chartHeight + 13, 9, COLORS.dim, false, MONO, 1);
    this.text('NOW', chartX + chartWidth, chartY + chartHeight + 13, 9, COLORS.dim, false, MONO, 1).setOrigin(1, 0);
  }

  private drawFooter(width: number, height: number): void {
    if (height < 640) return;
    this.text('OMEGA · OMEGA-1', 24, height - 28, 10, COLORS.dim, true, MONO, 1.4);
    this.text('LOCAL FIRST  ·  PRIVATE BY DEFAULT  ·  NO PROMPTS STORED', width - 24, height - 28, 9, COLORS.dim, false, MONO, 0.7).setOrigin(1, 0);
  }

  private panel(x: number, y: number, width: number, height: number, color: number): void {
    const shape = this.add.graphics();
    shape.fillStyle(color, 0.94);
    shape.fillRoundedRect(x, y, width, height, 10);
    shape.lineStyle(1, COLORS.line, 1);
    shape.strokeRoundedRect(x, y, width, height, 10);
    this.root.add(shape);
  }

  private rule(x1: number, y: number, x2: number, color: number): void {
    const shape = this.add.graphics();
    shape.lineStyle(1, color, 0.85);
    shape.lineBetween(x1, y, x2, y);
    this.root.add(shape);
  }

  private text(text: string, x: number, y: number, size: number, color: number, bold = false, font = FONT, letterSpacing = 0, wordWrapWidth?: number): Phaser.GameObjects.Text {
    const object = this.add.text(x, y, text, {
      color: `#${color.toString(16).padStart(6, '0')}`,
      fontFamily: font,
      fontSize: `${size}px`,
      fontStyle: bold ? 'bold' : 'normal',
      letterSpacing,
      wordWrap: wordWrapWidth ? { width: wordWrapWidth } : undefined,
    });
    this.root.add(object);
    return object;
  }

  private formatNumber(value: number): string {
    if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
    if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k`;
    return value.toString();
  }

  private formatClock(date: Date): string {
    const pad = (value: number) => value.toString().padStart(2, '0');
    return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  }

  private formatUptime(seconds: number): string {
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    return `${hours}h ${minutes.toString().padStart(2, '0')}m`;
  }
}
