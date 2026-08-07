import Phaser from 'phaser';
import './style.css';
import { TelemetryScene } from './scene';
import { demoSnapshot, startPolling } from './telemetry';
import type { TelemetrySnapshot } from './types';

const scene = new TelemetryScene(demoSnapshot());
const game = new Phaser.Game({
  type: Phaser.AUTO,
  parent: 'app',
  backgroundColor: '#08090d',
  render: { antialias: true, pixelArt: false, roundPixels: true },
  scale: {
    mode: Phaser.Scale.RESIZE,
    autoCenter: Phaser.Scale.CENTER_BOTH,
    width: window.innerWidth,
    height: window.innerHeight,
  },
  scene,
});

void game;

const setSnapshot = (snapshot: TelemetrySnapshot) => scene.setSnapshot(snapshot);
startPolling(setSnapshot, () => setSnapshot(demoSnapshot()));
