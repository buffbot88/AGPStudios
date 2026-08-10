import Phaser from 'phaser';
import type { LaneKey, LaneStatus, TelemetryFrame, TelemetrySnapshot } from './types';

const COLORS = {
  ink: 0x05070a,
  bgSoft: 0x0a0d12,
  panel: 0x0b0f15,
  line: 0x1c2431,
  lineSoft: 0x141a24,
  text: 0xe8edf4,
  textSoft: 0xa3aebd,
  muted: 0x6b7684,
  dim: 0x3d4754,
  accent: 0xff7a45,
  cyan: 0x39c2d6,
  green: 0x3ddc97,
  amber: 0xf2b23e,
  red: 0xff5d5d,
};

const FONT = 'Inter, ui-sans-serif, system-ui, sans-serif';
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
    y += this.drawHero(margin, y, contentWidth) + 14;

    const laneGap = 12;
    const cardWidth = (contentWidth - laneGap * 2) / 3;
    LANE_ORDER.forEach((key, index) => {
      const lane = this.snapshot.status.lanes[key];
      const cardX = margin + index * (cardWidth + laneGap);
      this.drawCompactLaneCard(cardX, y, cardWidth, lane, key);
    });
    y += compact ? 204 : 224;

    const chartFrames = this.snapshot.timeseries[this.selectedChartLane] ?? [];
    this.drawPerformanceChart(margin, y, contentWidth, this.selectedChartLane, chartFrames);

    this.drawFooter(width, height);
  }

  private drawBackdrop(width: number, height: number): void {
    const background = this.add.graphics();
    background.fillStyle(COLORS.ink, 1);
    background.fillRect(0, 0, width, height);
    // Faint engineering grid.
    background.lineStyle(1, COLORS.lineSoft, 0.5);
    const step = 56;
    for (let gx = step; gx < width; gx += step) background.lineBetween(gx, 0, gx, height);
    for (let gy = step; gy < height; gy += step) background.lineBetween(0, gy, width, gy);
    // Top glow strip for the HUD feel.
    background.fillStyle(COLORS.accent, 0.05);
    background.fillRect(0, 0, width, 2);
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
    this.text(' / NEURAL HOST', x + 118, y + 1, 16, COLORS.accent, false, FONT);
    this.text('V 6.2  ·  ASHAT NEURAL HOST · MASTER EDITION', x, y + 29, 9, COLORS.muted, false, MONO, 1.5);

    const rightX = x + width;
    this.statusDot = this.add.circle(rightX - 148, y + 13, 5, this.snapshot.status.all_ready ? COLORS.green : COLORS.amber);
    this.root.add(this.statusDot);
    this.connectionLabel = this.text(
      this.snapshot.demo ? 'DEMO FEED' : 'LIVE FEED',
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

  private systemStatusWord(): { word: string; color: number } {
    const HEALTHY = ['online', 'busy', 'waking'];
    const lanes = LANE_ORDER.map((key) => this.snapshot.status.lanes[key].lane_state);
    if (this.snapshot.status.degraded || lanes.includes('degraded')) {
      return { word: 'DEGRADED', color: COLORS.amber };
    }
    if (lanes.every((state) => HEALTHY.includes(state))) {
      return { word: 'NOMINAL', color: COLORS.green };
    }
    if (lanes.some((state) => state === 'online' || state === 'busy')) {
      return { word: 'PARTIAL', color: COLORS.amber };
    }
    return { word: 'OFFLINE', color: COLORS.red };
  }

  /** Hero status panel + the model ticker strip below it. Returns the total
   *  vertical advance so the caller can continue the layout. */
  private drawHero(x: number, y: number, width: number): number {
    const short = this.canvasHeight < 600;
    const heroHeight = short ? 112 : 132;
    this.panel(x, y, width, heroHeight, COLORS.panel);
    this.hudCorners(x, y, width, heroHeight, COLORS.accent);

    const logo = this.add.image(x + 42, y + 34, 'ashat-logo-large').setDisplaySize(58, 58).setAlpha(0.95);
    this.root.add(logo);
    this.text('PUBLIC TELEMETRY', x + 88, y + 12, 10, COLORS.cyan, true, MONO, 2.2);

    const status = this.systemStatusWord();
    this.lamp(x + 88, y + 54, status.color, 5);
    this.text(status.word, x + 102, y + 42, Math.min(24, width * 0.04), status.color, true, MONO, 2.4);
    this.text('SYSTEMS', x + 88, y + 74, 8, COLORS.dim, true, MONO, 2);

    const uptime = this.formatUptime(this.snapshot.status.uptime_seconds);
    const colX = x + width - 190;
    const lanesUp = LANE_ORDER.filter((key) => this.snapshot.status.lanes[key].lane_state !== 'offline').length;
    this.readout('UPTIME', uptime, colX, y + 8, COLORS.text);
    this.readout('ORCHESTRATOR', this.snapshot.status.orchestrator_pool.baseline_alive ? 'OK' : 'DOWN', colX, y + 44, this.snapshot.status.orchestrator_pool.baseline_alive ? COLORS.green : COLORS.red);
    if (!short) {
      this.readout('CAPACITY', `${lanesUp}/${LANE_ORDER.length} LANES`, colX, y + 80, COLORS.cyan);
    }

    // Active-model ticker: its own strip panel under the hero — never inside
    // it, so short viewports can't collide the readouts and ticker text.
    const stripY = y + heroHeight + 10;
    const stripH = 48;
    this.panel(x, stripY, width, stripH, COLORS.panel);
    this.hudCorners(x, stripY, width, stripH, COLORS.line);
    this.drawModelCard(x + 20, stripY + 5, x + width - 20);
    return heroHeight + 10 + stripH;
  }

  /** Right-aligned label/value readout pair (mission-console style). */
  private readout(label: string, value: string, rightX: number, y: number, color: number): void {
    this.text(label, rightX, y, 8, COLORS.dim, true, MONO, 1.6).setOrigin(1, 0);
    this.text(value, rightX, y + 14, 14, color, true, MONO, 0.6).setOrigin(1, 0);
  }

  /** HUD corner brackets. */
  private hudCorners(x: number, y: number, w: number, h: number, color: number): void {
    const len = 10;
    const g = this.add.graphics();
    g.lineStyle(1.5, color, 0.85);
    g.lineBetween(x, y + len, x, y);
    g.lineBetween(x, y, x + len, y);
    g.lineBetween(x + w - len, y, x + w, y);
    g.lineBetween(x + w, y, x + w, y + len);
    g.lineBetween(x, y + h - len, x, y + h);
    g.lineBetween(x, y + h, x + len, y + h);
    g.lineBetween(x + w - len, y + h, x + w, y + h);
    g.lineBetween(x + w, y + h - len, x + w, y + h);
    this.root.add(g);
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
    const cardHeight = this.canvasHeight < 600 ? 184 : 204;
    this.panel(x, y, width, cardHeight, COLORS.panel);

    const isOffline = lane.lane_state === 'offline';
    const stateColor = lane.lane_state === 'online' ? COLORS.green : lane.lane_state === 'degraded' ? COLORS.red : isOffline ? COLORS.dim : COLORS.amber;

    // Lane number + label with lamp.
    this.lamp(x + 16, y + 20, stateColor, 3.5);
    const number = (LANE_ORDER.indexOf(key) + 1).toString().padStart(2, '0');
    this.text(`${number}  ${lane.label.toUpperCase()}`, x + 28, y + 12, 10, COLORS.text, true, MONO, 1.4);

    const stateText = isOffline ? 'OFFLINE' : lane.lane_state.toUpperCase();
    this.text(stateText, x + width - 16, y + 16, 9, stateColor, true, MONO, 1.2).setOrigin(1, 0);

    if (isOffline) {
      this.text('NO CONNECTION', x + 16, y + 52, 11, COLORS.dim, false, MONO, 0.8);
      const url = LANE_URLS[key];
      if (url) {
        this.text(url.replace('https://', ''), x + 16, y + 72, 8, COLORS.dim, false, MONO, 0.3);
      }
    } else {
      this.text(this.friendlyModel(lane.model), x + 16, y + 48, 10, COLORS.textSoft, false, MONO, 0.1, width - 32);
    }

    this.rule(x + 16, y + 88, x + width - 16, COLORS.line);

    if (!isOffline) {
      const metricsCol1 = [
        ['REQUESTS', `${lane.total_requests.toLocaleString()}`],
        ['TOKENS', `${this.formatNumber(lane.total_prompt_tokens + lane.total_completion_tokens)}`],
      ];
      const metricsCol2 = [
        ['LATENCY', `${lane.avg_time_to_first_token_ms?.toFixed(0) ?? '—'}ms`],
        ['SPEED', `${lane.avg_generation_tokens_per_second.toFixed(1)} t/s`],
      ];

      const colW = (width - 32) / 2;
      metricsCol1.forEach(([label, value], i) => {
        this.text(label, x + 16, y + 100 + i * 40, 8, COLORS.dim, true, MONO, 0.8);
        this.text(value, x + 16, y + 112 + i * 40, 14, i === 0 ? COLORS.text : COLORS.cyan, true, MONO);
      });
      metricsCol2.forEach(([label, value], i) => {
        this.text(label, x + 16 + colW, y + 100 + i * 40, 8, COLORS.dim, true, MONO, 0.8);
        this.text(value, x + 16 + colW, y + 112 + i * 40, 14, COLORS.text, true, MONO);
      });

      // Success rate bar.
      const barW = width - 32;
      const barY = y + cardHeight - 20;
      const bar = this.add.graphics();
      bar.fillStyle(COLORS.line, 1);
      bar.fillRect(x + 16, barY, barW, 3);
      bar.fillStyle(lane.success_rate >= 98 ? COLORS.green : lane.success_rate >= 90 ? COLORS.amber : COLORS.red, 1);
      bar.fillRect(x + 16, barY, Math.max(2, (barW * lane.success_rate) / 100), 3);
      this.root.add(bar);
      this.text(`${lane.success_rate.toFixed(1)}% OK`, x + 16, barY - 16, 8, COLORS.muted, false, MONO, 0.6);
    } else {
      this.text('STANDBY', x + 16, y + 116, 14, COLORS.dim, true, MONO, 1);
    }
  }

  /** Status lamp: filled dot with a soft halo. */
  private lamp(x: number, y: number, color: number, radius: number): void {
    const g = this.add.graphics();
    g.fillStyle(color, 0.22);
    g.fillCircle(x, y, radius * 2.4);
    g.fillStyle(color, 1);
    g.fillCircle(x, y, radius);
    this.root.add(g);
  }

  private laneColor(key: LaneKey): number {
    if (key === 'beta') return COLORS.cyan;
    if (key === 'delta') return COLORS.amber;
    return COLORS.accent;
  }

  private drawPerformanceChart(x: number, y: number, width: number, laneKey: LaneKey, frames: TelemetryFrame[]): void {
    const height = 212;
    const laneColor = this.laneColor(laneKey);
    this.panel(x, y, width, height, COLORS.panel);
    this.hudCorners(x, y, width, height, COLORS.line);
    this.lamp(x + 20, y + 20, laneColor, 3);
    this.text(`GENERATION VELOCITY  ·  ${laneKey.toUpperCase()}`, x + 34, y + 12, 10, laneColor, true, MONO, 1.5);

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
    this.text('OMEGA V6.2 · MASTER EDITION', 24, height - 28, 10, COLORS.dim, true, MONO, 1.4);
    this.text('LOCAL FIRST  ·  PRIVATE BY DEFAULT  ·  NO PROMPTS STORED', width - 24, height - 28, 9, COLORS.dim, false, MONO, 0.7).setOrigin(1, 0);
  }

  private panel(x: number, y: number, width: number, height: number, color: number): void {
    const shape = this.add.graphics();
    shape.fillStyle(color, 0.96);
    shape.fillRoundedRect(x, y, width, height, 8);
    shape.lineStyle(1, COLORS.line, 1);
    shape.strokeRoundedRect(x, y, width, height, 8);
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
