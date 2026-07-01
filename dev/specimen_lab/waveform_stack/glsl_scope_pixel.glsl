// Waveform Stack -- N vertically-stacked scope lanes via an O(1) row-fold. No trace loop.
uniform vec4 uWave;    // x=lanes, y=frequency, z=amplitude, w=time(=absTime*Speed)
uniform vec4 uSweep;   // x=sweepRate, y=afterglow, z=ptpRate, w=lineWidth
uniform vec4 uLook;    // x=graticule, y=harmonics, z=glow, w=absTime
out vec4 fragColor;
const float PI = 3.14159265;

// a rich broadcast-like signal: summed detuned harmonics, per-lane seeded
float signal(float x, float seed, float freq, float harm, float t){
    float v = 0.0;
    v += sin(x*freq + t + seed);
    v += 0.5  * sin(x*freq*2.03 + t*1.3 + seed*1.7);
    v += 0.33 * sin(x*freq*3.97 + t*0.8 + seed*2.3) * harm;
    v += 0.22 * sin(x*freq*7.1  - t*1.1 + seed*0.5) * harm;
    return v / 2.0;   // ~[-1,1]
}

vec3 laneColor(float i){
    // bottom lane green (audio), then cyan (video), magenta (chroma), amber, repeat
    float k = mod(i, 4.0);
    if(k < 0.5) return vec3(0.18, 1.00, 0.40);   // green audio
    if(k < 1.5) return vec3(0.00, 0.85, 1.00);   // cyan video
    if(k < 2.5) return vec3(1.00, 0.18, 0.60);   // magenta chroma
    return vec3(1.00, 0.70, 0.10);               // amber
}

void main(){
    vec2 uv = vUV.st;
    float aspect = uTDOutputInfo.res.z / uTDOutputInfo.res.w;

    vec3 col = vec3(0.010, 0.012, 0.016);   // near-black scope ground

    float nL   = clamp(uWave.x, 1.0, 512.0);
    float freq = uWave.y;
    float amp  = uWave.z;
    float harm = uLook.y;
    float t    = uWave.w;
    float lw   = uSweep.w;
    float glow = uLook.z;
    float grat = uLook.x;

    // --- gold graticule: horizontal IRE divisions + vertical divisions ---
    vec2 g = abs(fract(uv * vec2(8.0, nL*3.0)) - 0.5);
    float grid = smoothstep(0.49, 0.5, max(g.x, g.y));
    col += vec3(0.20, 0.16, 0.05) * grid * 0.16 * grat;

    // --- O(1) row-fold: which lane is this pixel in, and where within it ---
    float laneF = uv.y * nL;        // 0..nL (bottom..top)
    float li    = floor(laneF);
    float li_c  = clamp(li, 0.0, nL-1.0);
    float ly    = fract(laneF);     // 0..1 within the lane
    // lane divider hairline (crisp via fwidth)
    float aaY = fwidth(laneF);
    float divider = smoothstep(aaY*1.5, 0.0, min(ly, 1.0-ly));
    col += vec3(0.18,0.15,0.05) * divider * 0.12 * grat;

    // --- the one analytic trace in this lane ---
    vec3 lc   = laneColor(li_c);
    float seed = li_c*2.1 + 0.3;
    float ampL = mix(1.0, 0.55, mod(li_c,2.0));   // alternate lane swing a touch
    float s    = signal((uv.x*2.0-1.0)*aspect, seed, freq, harm, t);
    float yc   = 0.5 + s * amp * ampL * 0.34;     // center of the lane + signal swing
    float d    = abs(ly - yc);
    float aa   = fwidth(ly) * 1.5;
    float line = smoothstep(lw+aa, lw-aa, d);      // crisp hairline core at any res
    float halo = smoothstep(lw*8.0*glow, 0.0, d) * 0.32;

    // --- sweeping white-hot vertical playhead across ALL lanes + phosphor afterglow ---
    float head = fract(uLook.w * uSweep.x);
    float ageBehind = head - uv.x;
    if(ageBehind < 0.0) ageBehind += 1.0;
    float persistence = exp(-ageBehind * mix(8.0, 1.5, uSweep.y));
    float bright = (line + halo) * persistence;
    vec3 c = mix(lc, vec3(1.0), line * smoothstep(0.06, 0.0, ageBehind));   // white at the head
    col += c * bright;
    // the playhead column itself glows white-hot
    float headCol = smoothstep(0.004, 0.0, abs(uv.x - head));
    col += vec3(0.8,0.95,1.0) * headCol * line * 1.2;

    // --- PTP beat: stack flashes + a horizontal sync bar wipes DOWN ---
    float beat = fract(uLook.w * uSweep.z);
    float flash = pow(max(0.0, 1.0 - beat*4.0), 3.0);
    col += vec3(0.05, 0.08, 0.10) * flash;
    float bar = smoothstep(0.006, 0.0, abs(uv.y - (1.0-beat)));   // wipes top->bottom
    col += vec3(0.10, 0.20, 0.24) * bar * 0.6;

    fragColor = vec4(max(col, 0.0), 1.0);
}
