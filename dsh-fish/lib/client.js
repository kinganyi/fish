window.__ModuleLoader__.load({
	id: "dsh-fish",
	factory: (require) => {
		var module = { exports: {} };
		var exports = module.exports;
		Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });
		let react = require("react");
		//#region src/client/index.ts
		const React = {
			createElement: react.createElement,
			useEffect: react.useEffect,
			useRef: react.useRef
		};
		/** 与宿主半同名的插件 id。 */
		const name = "dsh-fish";
		/** 面板 id：三处槽位（内嵌/整页/侧栏）共用。 */
		const PANEL_ID = "fish-scene";
		const TAU = Math.PI * 2;
		const clamp = (v, a, b) => v < a ? a : v > b ? b : v;
		/** 3D 透视海面的参数（与第一期同一组值） */
		const SEA = {
			focal: 1180,
			camH: 118,
			rows: 26,
			horizonRatio: .62
		};
		function waveH(x, z, t) {
			const s1 = Math.sin(x * .0034 + z * .0021 + t * .55) * 17;
			const s2 = Math.sin(x * .0086 - z * .0059 + t * .42) * 10.2;
			const s3 = Math.sin(x * .0207 + z * .0138 + t * .98) * 4.6;
			const s4 = Math.sin(x * .0475 - z * .0292 + t * 1.74) * 2;
			const s5 = Math.sin(z * .0071 + t * .19) * 5.8;
			return s1 + s2 + s3 + s4 + s5;
		}
		/** 演示用的原子分布：真数据接进来之前，先用它证明管线 */
		const ATOMS = [
			{
				x01: .3,
				y01: .34,
				r: 46,
				hue: 174,
				phase: 0
			},
			{
				x01: .52,
				y01: .26,
				r: 40,
				hue: 174,
				phase: 1.7
			},
			{
				x01: .44,
				y01: .52,
				r: 38,
				hue: 174,
				phase: 3.1
			},
			{
				x01: .68,
				y01: .4,
				r: 34,
				hue: 258,
				phase: 4.4
			},
			{
				x01: .82,
				y01: .3,
				r: 30,
				hue: 34,
				phase: 5.6
			}
		];
		function renderScene(canvas, t, size) {
			const ctx = canvas.getContext("2d");
			if (!ctx) return;
			const { w, h, dpr } = size;
			ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
			ctx.clearRect(0, 0, w, h);
			const bg = ctx.createRadialGradient(w * .55, h * .34, 0, w * .55, h * .5, Math.max(w, h) * .95);
			bg.addColorStop(0, "#0b1a29");
			bg.addColorStop(.38, "#071019");
			bg.addColorStop(.72, "#040910");
			bg.addColorStop(1, "#020409");
			ctx.fillStyle = bg;
			ctx.fillRect(0, 0, w, h);
			const horizon = h * SEA.horizonRatio;
			const tide = .8 + .2 * Math.sin(t * TAU / 26);
			ctx.fillStyle = "rgba(96,222,232,1)";
			const top = horizon + 3;
			const bottom = h * 1.04;
			for (let i = 0; i < SEA.rows; i++) {
				const u = i / (SEA.rows - 1);
				const yy = top + Math.pow(u, 1.22) * (bottom - top);
				const dz = Math.max(3, yy - horizon);
				const z = SEA.camH * SEA.focal / dz;
				const depth = u;
				const scale = SEA.focal / z;
				const stepPx = 16 - 9 * Math.pow(depth, .55);
				const rowVis = (.17 + Math.pow(depth, .72) * 1.02) * tide;
				const pxSize = Math.max(1.05, scale * 1.45);
				const halfW = w * .5;
				for (let sx = 0; sx <= w; sx += stepPx) {
					const hh = waveH((sx - halfW) / scale, z, t);
					const hScale = .55 + .45 * (1 - depth);
					const sy = yy - hh * scale * hScale;
					const crest = clamp((hh + 36) / 72, 0, 1);
					const a = rowVis * (.16 + crest * 1.55) * (.86 + .52 * clamp((crest - .62) / .38, 0, 1));
					if (a < .012) continue;
					ctx.globalAlpha = a > 1 ? 1 : a;
					ctx.fillRect(sx, sy, pxSize, pxSize);
				}
			}
			ctx.globalAlpha = 1;
			const hl = ctx.createLinearGradient(0, 0, w, 0);
			hl.addColorStop(0, "rgba(140,238,244,0)");
			hl.addColorStop(.5, "rgba(190,250,252," + (.5 * tide).toFixed(3) + ")");
			hl.addColorStop(1, "rgba(140,238,244,0)");
			ctx.fillStyle = hl;
			ctx.fillRect(0, horizon - .6, w, 1.2);
			for (const a of ATOMS) {
				const bobX = Math.sin(t * .61 + a.phase) * 4.6;
				const bobY = Math.cos(t * .47 + a.phase * .8) * 3.8;
				const x = a.x01 * w + bobX;
				const y = a.y01 * h + bobY;
				const r = a.r;
				const hue = a.hue;
				const pulse = .5 + .5 * Math.sin(t * TAU / 4.2 + a.phase);
				const halo = ctx.createRadialGradient(x, y, r * .2, x, y, r * 2.4);
				halo.addColorStop(0, `hsla(${hue},72%,62%,0.22)`);
				halo.addColorStop(.5, `hsla(${hue},70%,55%,0.08)`);
				halo.addColorStop(1, `hsla(${hue},70%,50%,0)`);
				ctx.fillStyle = halo;
				ctx.beginPath();
				ctx.arc(x, y, r * 2.4, 0, TAU);
				ctx.fill();
				const body = ctx.createRadialGradient(x - r * .34, y - r * .36, r * .04, x, y, r);
				body.addColorStop(0, `hsla(${hue},66%,68%,0.52)`);
				body.addColorStop(.26, `hsla(${hue},62%,52%,0.44)`);
				body.addColorStop(.58, `hsla(${hue},58%,38%,0.34)`);
				body.addColorStop(1, `hsla(${hue},52%,22%,0)`);
				ctx.fillStyle = body;
				ctx.beginPath();
				ctx.arc(x, y, r, 0, TAU);
				ctx.fill();
				const core = ctx.createRadialGradient(x, y, 0, x, y, r * .24);
				core.addColorStop(0, `rgba(255,255,255,${(.9 * (.6 + pulse * .4)).toFixed(3)})`);
				core.addColorStop(.4, `hsla(${hue},90%,84%,0.32)`);
				core.addColorStop(1, `hsla(${hue},88%,72%,0)`);
				ctx.fillStyle = core;
				ctx.beginPath();
				ctx.arc(x, y, r * .24, 0, TAU);
				ctx.fill();
				ctx.textAlign = "center";
				ctx.textBaseline = "middle";
				ctx.font = `${clamp(r * .22, 7, 11).toFixed(1)}px ui-monospace, Consolas, monospace`;
				for (let k = 0; k < 14; k++) {
					const ang = k / 14 * TAU + t * .22 + a.phase;
					const rr = r * (.94 + .24 * Math.sin(k * 2.3 + t * 2.2));
					const px = x + Math.cos(ang) * rr;
					const py = y + Math.sin(ang) * rr * .96;
					const life = .35 + .65 * Math.abs(Math.sin(k * 1.7 + t * 1.5 + a.phase));
					ctx.globalAlpha = life * .7;
					ctx.lineWidth = 1.6;
					ctx.strokeStyle = `rgba(2,5,10,${(life * .5).toFixed(3)})`;
					const ch = k % 2 ? "1" : "0";
					ctx.strokeText(ch, px, py);
					ctx.fillStyle = `hsl(${hue},92%,88%)`;
					ctx.fillText(ch, px, py);
				}
				ctx.globalAlpha = 1;
				ctx.textAlign = "start";
				for (let o = 0; o < 2; o++) {
					const tilt = (o - .5) * .7;
					const sq = .28 + o * .12;
					const orbR = r * (1.5 + o * .16);
					ctx.globalAlpha = .12;
					ctx.strokeStyle = `hsl(${hue},60%,68%)`;
					ctx.lineWidth = .75;
					ctx.beginPath();
					ctx.ellipse(x, y, orbR, orbR * sq, tilt, 0, TAU);
					ctx.stroke();
					const ang = t * (.9 + o * .3) + a.phase + o * Math.PI;
					const ex0 = Math.cos(ang) * orbR;
					const ey0 = Math.sin(ang) * orbR * sq;
					const px = x + ex0 * Math.cos(tilt) - ey0 * Math.sin(tilt);
					const py = y + ex0 * Math.sin(tilt) + ey0 * Math.cos(tilt);
					const front = .45 + .55 * clamp((Math.sin(ang) + 1) / 2, 0, 1);
					ctx.globalAlpha = .85 * front;
					ctx.fillStyle = "rgba(255,255,255,0.95)";
					ctx.beginPath();
					ctx.arc(px, py, r * .045 * front, 0, TAU);
					ctx.fill();
				}
				ctx.globalAlpha = 1;
			}
		}
		function makePanel(React) {
			const { createElement: h, useEffect, useRef } = React;
			return function FishPanel() {
				const ref = useRef(null);
				useEffect(() => {
					const cv = ref.current;
					if (!cv) return;
					let raf = 0;
					let alive = true;
					const t0 = performance.now();
					const size = {
						w: 0,
						h: 0,
						dpr: 1
					};
					const resize = () => {
						const dpr = Math.min(window.devicePixelRatio || 1, 2);
						const rect = cv.getBoundingClientRect();
						size.w = Math.max(1, Math.floor(rect.width));
						size.h = Math.max(1, Math.floor(rect.height));
						size.dpr = dpr;
						cv.width = Math.floor(size.w * dpr);
						cv.height = Math.floor(size.h * dpr);
					};
					resize();
					const loop = (now) => {
						if (!alive) return;
						renderScene(cv, (now - t0) / 1e3, size);
						raf = requestAnimationFrame(loop);
					};
					raf = requestAnimationFrame(loop);
					let ro = null;
					if (typeof ResizeObserver !== "undefined") {
						ro = new ResizeObserver(() => resize());
						ro.observe(cv);
					} else window.addEventListener("resize", resize);
					return () => {
						alive = false;
						cancelAnimationFrame(raf);
						if (ro) ro.disconnect();
						else window.removeEventListener("resize", resize);
					};
				}, []);
				return h("div", { style: {
					flex: "1 1 0",
					minHeight: 0,
					position: "relative",
					overflow: "hidden"
				} }, h("canvas", {
					ref,
					style: {
						position: "absolute",
						top: 0,
						left: 0,
						right: 0,
						bottom: 0,
						width: "100%",
						height: "100%",
						display: "block",
						borderRadius: "3px",
						background: "#020409"
					}
				}));
			};
		}
		/**
		* 客户端半的装配入口。
		*
		* `inject` 是**服务边**：`slots` 由官方 `dsh-client-ui-renderer` 提供。
		* 它不在 → 本插件不激活 → 面板不注册（而不是崩掉）。
		* 注意这与 package.json 的 `dsh.client.inject` 是**两个不同层**的东西：
		* 那边是模块表行边（包名，决定加载序），这边是 cordis 服务依赖。
		*/
		const inject = ["slots"];
		function apply(ctx) {
			ctx.provide("fishClient", {
				pluginId: name,
				viewModelVersion: "v1"
			});
			const slots = ctx.slots;
			slots.inject("conversation.view", () => {
				const Panel = makePanel(React);
				return slots.register({
					name: "conversation.view",
					id: PANEL_ID,
					order: 96,
					label: () => "fish"
				}, Panel);
			});
		}
		//#endregion
		exports.PANEL_ID = PANEL_ID;
		exports.apply = apply;
		exports.inject = inject;
		exports.name = name;
		return module.exports;
	}
});

//# sourceMappingURL=client.js.map