const { useEffect, useRef } = React;

// Adapted from React Bits' Magic Rings shader:
// https://reactbits.dev/animations/magic-rings
//
// This compact variant talks directly to WebGL2 instead of bundling Three.js.
// It renders at most 30 FPS while visible; inactive states render once and stop.
const VERTEX_SHADER = `#version 300 es
in vec2 aPosition;

void main() {
  gl_Position = vec4(aPosition, 0.0, 1.0);
}
`;

const FRAGMENT_SHADER = `#version 300 es
precision highp float;

uniform float uTime, uAttenuation, uLineThickness;
uniform float uBaseRadius, uRadiusStep, uScaleRate;
uniform float uOpacity, uNoiseAmount, uRingGap;
uniform float uFadeIn, uFadeOut;
uniform vec2 uResolution;
uniform vec3 uColor, uColorTwo;
uniform int uRingCount;

out vec4 outColor;

const float HALF_PI = 1.5707963;
const float CYCLE = 3.45;

float fade(float t) {
  return t < uFadeIn
    ? smoothstep(0.0, uFadeIn, t)
    : 1.0 - smoothstep(uFadeOut, CYCLE - 0.2, t);
}

float ring(vec2 p, float radius, float cut, float timeOffset, float px) {
  float t = mod(uTime + timeOffset, CYCLE);
  float r = radius + t / CYCLE * uScaleRate;
  float d = abs(length(p) - r);
  float angle = atan(abs(p.y), abs(p.x)) / HALF_PI;
  float thickness = max(1.0 - angle, 0.5) * px * uLineThickness;
  float highlight = (1.0 - smoothstep(thickness, thickness * 1.5, d)) + 1.0;
  d += pow(cut * angle, 3.0) * r;
  return highlight * exp(-uAttenuation * d) * fade(t);
}

void main() {
  float px = 1.0 / min(uResolution.x, uResolution.y);
  vec2 p = (gl_FragCoord.xy - 0.5 * uResolution.xy) * px;
  // Preserve the source demo's ~1.9:1 composition inside this wider status slot.
  // Without this fit, the circles collapse into one tiny central halo.
  p.x *= min(1.0, 1.9 / (uResolution.x / uResolution.y));
  vec3 color = vec3(0.0);
  float ringCount = max(float(uRingCount) - 1.0, 1.0);

  for (int i = 0; i < 8; i++) {
    if (i >= uRingCount) break;
    float fi = float(i);
    vec3 ringColor = mix(uColor, uColorTwo, fi / ringCount);
    float strength = ring(
      p,
      uBaseRadius + fi * uRadiusStep,
      pow(uRingGap, fi),
      i == 0 ? 0.0 : 2.95 * fi,
      px
    );
    color = mix(color, ringColor, vec3(strength));
  }

  float noise = fract(sin(dot(gl_FragCoord.xy + uTime * 100.0, vec2(12.9898, 78.233))) * 43758.5453);
  color += (noise - 0.5) * uNoiseAmount;
  outColor = vec4(color, max(color.r, max(color.g, color.b)) * uOpacity);
}
`;

function compileShader(gl, type, source) {
  const shader = gl.createShader(type);
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (gl.getShaderParameter(shader, gl.COMPILE_STATUS)) return shader;
  gl.deleteShader(shader);
  return null;
}

function parseHexColor(hex) {
  const value = String(hex || "").replace("#", "");
  if (!/^[0-9a-f]{6}$/i.test(value)) return [1, 1, 1];
  return [0, 2, 4].map(offset => parseInt(value.slice(offset, offset + 2), 16) / 255);
}

function MagicRingsCanvas({ active, color, colorTwo }) {
  const canvasRef = useRef(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return undefined;

    const gl = canvas.getContext("webgl2", {
      alpha: true,
      antialias: false,
      depth: false,
      powerPreference: "low-power",
      premultipliedAlpha: true,
    });
    if (!gl) return undefined;

    const vertex = compileShader(gl, gl.VERTEX_SHADER, VERTEX_SHADER);
    const fragment = compileShader(gl, gl.FRAGMENT_SHADER, FRAGMENT_SHADER);
    if (!vertex || !fragment) {
      if (vertex) gl.deleteShader(vertex);
      if (fragment) gl.deleteShader(fragment);
      return undefined;
    }

    const program = gl.createProgram();
    gl.attachShader(program, vertex);
    gl.attachShader(program, fragment);
    gl.linkProgram(program);
    gl.deleteShader(vertex);
    gl.deleteShader(fragment);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      gl.deleteProgram(program);
      return undefined;
    }

    const buffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([
      -1, -1, 1, -1, -1, 1, 1, 1,
    ]), gl.STATIC_DRAW);

    gl.useProgram(program);
    const position = gl.getAttribLocation(program, "aPosition");
    gl.enableVertexAttribArray(position);
    gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);

    const uniform = name => gl.getUniformLocation(program, name);
    const uniforms = {
      time: uniform("uTime"),
      attenuation: uniform("uAttenuation"),
      lineThickness: uniform("uLineThickness"),
      baseRadius: uniform("uBaseRadius"),
      radiusStep: uniform("uRadiusStep"),
      scaleRate: uniform("uScaleRate"),
      opacity: uniform("uOpacity"),
      noiseAmount: uniform("uNoiseAmount"),
      ringGap: uniform("uRingGap"),
      fadeIn: uniform("uFadeIn"),
      fadeOut: uniform("uFadeOut"),
      resolution: uniform("uResolution"),
      color: uniform("uColor"),
      colorTwo: uniform("uColorTwo"),
      ringCount: uniform("uRingCount"),
    };

    const firstColor = parseHexColor(color);
    const secondColor = parseHexColor(colorTwo);
    let visible = true;
    let timer = 0;
    let elapsed = active ? 0 : 0.82;
    let previous = performance.now();
    let disposed = false;

    const prefersReducedMotion = window.matchMedia
      ? window.matchMedia("(prefers-reduced-motion: reduce)")
      : null;

    const resize = () => {
      const rect = canvas.getBoundingClientRect();
      const pixelRatio = Math.min(window.devicePixelRatio || 1, 1.5);
      const width = Math.max(1, Math.round(rect.width * pixelRatio));
      const height = Math.max(1, Math.round(rect.height * pixelRatio));
      if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width;
        canvas.height = height;
        gl.viewport(0, 0, width, height);
      }
    };

    const draw = () => {
      if (disposed || gl.isContextLost()) return;
      resize();
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      gl.useProgram(program);
      gl.uniform1f(uniforms.time, elapsed);
      // Keep the React Bits source defaults; only frame rate and colors are adapted.
      gl.uniform1f(uniforms.attenuation, 10);
      gl.uniform1f(uniforms.lineThickness, 2);
      gl.uniform1f(uniforms.baseRadius, 0.35);
      gl.uniform1f(uniforms.radiusStep, 0.1);
      gl.uniform1f(uniforms.scaleRate, 0.1);
      gl.uniform1f(uniforms.opacity, 1);
      gl.uniform1f(uniforms.noiseAmount, 0.1);
      gl.uniform1f(uniforms.ringGap, 1.5);
      gl.uniform1f(uniforms.fadeIn, 0.7);
      gl.uniform1f(uniforms.fadeOut, 0.5);
      gl.uniform2f(uniforms.resolution, canvas.width, canvas.height);
      gl.uniform3fv(uniforms.color, firstColor);
      gl.uniform3fv(uniforms.colorTwo, secondColor);
      gl.uniform1i(uniforms.ringCount, 6);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    };

    const shouldAnimate = () => active
      && visible
      && !document.hidden
      && !(prefersReducedMotion && prefersReducedMotion.matches);

    const stop = () => {
      if (timer) window.clearTimeout(timer);
      timer = 0;
    };

    const tick = () => {
      stop();
      if (!shouldAnimate()) return;
      const now = performance.now();
      elapsed += Math.min(now - previous, 100) * 0.001;
      previous = now;
      draw();
      timer = window.setTimeout(tick, 1000 / 30);
    };

    const refresh = () => {
      stop();
      previous = performance.now();
      if (shouldAnimate()) tick();
      else draw();
    };

    const resizeObserver = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(draw);
    if (resizeObserver) resizeObserver.observe(canvas);
    const intersectionObserver = typeof IntersectionObserver === "undefined" ? null : new IntersectionObserver(([entry]) => {
      visible = entry.isIntersecting;
      refresh();
    }, { threshold: 0 });
    if (intersectionObserver) intersectionObserver.observe(canvas);
    document.addEventListener("visibilitychange", refresh);
    if (prefersReducedMotion) prefersReducedMotion.addEventListener("change", refresh);
    refresh();

    return () => {
      disposed = true;
      stop();
      document.removeEventListener("visibilitychange", refresh);
      if (prefersReducedMotion) prefersReducedMotion.removeEventListener("change", refresh);
      if (resizeObserver) resizeObserver.disconnect();
      if (intersectionObserver) intersectionObserver.disconnect();
      gl.deleteBuffer(buffer);
      gl.deleteProgram(program);
    };
  }, [active, color, colorTwo]);

  return <canvas className="execution-rings-canvas" ref={canvasRef} aria-hidden="true" />;
}

export function ExecutionStatusRings({ status, executionState, live }) {
  const draining = live && executionState === "draining";
  const reconcileRequired = live && executionState === "reconcile_required";
  const stopped = status === "stopped";
  const paused = status === "paused" && !draining && !reconcileRequired;
  const running = !stopped && !paused && !draining && !reconcileRequired;

  let state = running ? (live ? "live" : "paper") : "stopped";
  let label = running ? (live ? "LIVE" : "PAPER") : "STOP";
  let detail = running ? (live ? "实盘跟单运行中" : "模拟跟单运行中") : "跟单已停止";
  let color = "#55d5b2";
  let colorTwo = "#46adbc";
  let icon = null;

  if (paused || draining) {
    state = "paused";
    label = draining ? "DRAIN" : "PAUSE";
    detail = draining ? "实盘正在排空，只维护已有持仓" : "已暂停新开仓，继续维护已有持仓";
    color = "#e7c56f";
    colorTwo = "#8f6d32";
    icon = "Ⅱ";
  } else if (reconcileRequired) {
    state = "check";
    label = "CHECK";
    detail = "需要先完成账户核对";
    color = "#ff8a8a";
    colorTwo = "#7e303d";
  } else if (stopped) {
    color = "#7a7a82";
    colorTwo = "#34343a";
  }

  return (
    <div className={`execution-rings-status ${state}`} role="status" aria-live="polite"
      aria-label={detail} title={detail}>
      <MagicRingsCanvas active={running} color={color} colorTwo={colorTwo} />
      <span className="execution-rings-label">
        {icon && <i aria-hidden="true">{icon}</i>}
        {label}
      </span>
    </div>
  );
}
