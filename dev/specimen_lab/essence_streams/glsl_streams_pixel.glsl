// Essence Streams -- three vertical signal channels via an O(1) band-fold. No loops.
uniform vec4 uChan;   // x=streams, y=gutter, z=edgeGlow, w=cellDensity
uniform vec4 uFlow;   // x=flowSpeed, y=filaments, z=shimmer, w=time(=absTime*flowSpeed)
uniform vec4 uLook;   // x=ptpRate, y=syncLine, z=brightness, w=absTime
out vec4 fragColor;
const float PI = 3.14159265;

float hash21(vec2 p){ return fract(sin(dot(p, vec2(127.1,311.7)))*43758.5453); }
// cheap 2D value noise (single lookup, smooth) -- no octave loop
float vnoise(vec2 p){
    vec2 i=floor(p), f=fract(p); vec2 u=f*f*(3.0-2.0*f);
    return mix(mix(hash21(i), hash21(i+vec2(1,0)), u.x),
               mix(hash21(i+vec2(0,1)), hash21(i+vec2(1,1)), u.x), u.y);
}

vec3 channelColor(float c){
    if(c < 0.5) return vec3(0.00, 0.85, 1.00);   // cyan video
    if(c < 1.5) return vec3(0.22, 1.00, 0.42);   // green audio
    return vec3(1.00, 0.69, 0.00);               // amber ancillary
}

void main(){
    vec2 uv = vUV.st;             // 0..1, y up
    float nC   = clamp(uChan.x, 1.0, 3.0);
    float gut  = uChan.y;         // gutter (dark gap) width fraction
    float eg   = uChan.z;         // edge glow
    float cd   = uChan.w;         // cell density
    float fspd = uFlow.x;
    float fil  = uFlow.y;
    float shim = uFlow.z;
    float t    = uFlow.w;
    float bright = uLook.z;

    vec3 col = vec3(0.010, 0.012, 0.016);   // near-black ground

    // --- O(1) band-fold: which channel is this pixel in, and where across it ---
    float bandF = uv.x * nC;        // 0..nC
    float ci    = floor(bandF);     // channel index
    float ci_c  = clamp(ci, 0.0, nC-1.0);
    float lx    = fract(bandF);     // 0..1 across the channel

    // gutter mask: dark hairline gaps between channels (and at the frame edges)
    float halfGut = gut*0.5;
    float inBand  = smoothstep(0.0, 0.02, lx-halfGut) * smoothstep(1.0, 0.98, lx+halfGut);
    // crisp channel edge with fwidth for resolution independence
    float aa = fwidth(lx) * 1.5;
    float edgeL = smoothstep(halfGut+aa, halfGut-aa, abs(lx-halfGut));
    float edgeR = smoothstep(halfGut+aa, halfGut-aa, abs(lx-(1.0-halfGut)));

    vec3 cc = channelColor(ci_c);

    // --- BODY: per-channel sub-lanes, each with its OWN speed (wide variety)
    // and activity. Many lanes -> many speeds (slow + fast); quiet lanes give
    // NEGATIVE SPACE; sparse bright packets give contrast + LARGE detail; fine
    // moire gives SMALL detail. Multi-scale hierarchy on a mostly-dark ground.
    float K = 6.0;                                   // sub-lanes across the channel
    float subN = floor(lx * K);
    float subF = fract(lx * K);
    float ls = hash21(vec2(subN, ci_c*7.0 + 1.0));   // per-lane seed
    float laneSpeed = mix(0.12, 2.6, ls*ls);         // WIDE speed range, biased slow
    float laneAct = smoothstep(0.42, 0.80, hash21(vec2(subN + 17.0, ci_c)));  // some lanes near-dark
    float ly = uv.y + t * laneSpeed;                 // this lane's own flow
    float laneEdge = smoothstep(0.5, 0.5 - fwidth(lx*K)*1.5 - 0.04, abs(subF-0.5)); // lane gutters

    // small detail: near-frequency micro-columns -> moire, gated by lane activity
    float colsA = 9.0 + 12.0*cd;
    float aav   = fwidth(lx*colsA)*1.4;
    float micro = smoothstep(0.5, 0.4-aav, abs(fract(lx*colsA + ly*0.4)-0.5))
                * smoothstep(0.5, 0.4-aav, abs(fract(lx*colsA*1.08 - ly*0.25)-0.5));
    // smallest detail: fine fast scan-lines
    float scanF = 80.0 + 120.0*cd;
    float scan  = smoothstep(0.5, 0.4 - fwidth(ly*scanF)*1.4, abs(fract(ly*scanF)-0.5));

    // LARGE detail: sparse bright packet blocks marching at the lane's own speed
    float pkScale = mix(5.0, 14.0, ls);
    float pkId = floor(ly*pkScale);
    float pkOn = step(0.6, hash21(vec2(pkId, subN + ci_c*3.0)));     // sparse -> negative space
    float pkF  = fract(ly*pkScale);
    float pkBody = pkOn * smoothstep(0.06,0.16,pkF) * (1.0 - smoothstep(0.6,0.82,pkF));
    float pkBright = 0.5 + 0.5*hash21(vec2(pkId+3.0, subN));

    float n1 = vnoise(vec2(lx*5.0, ly*3.0));                          // 3rd-scale break-up
    float fine = (micro*0.55 + scan*0.35) * laneAct;
    float detail = fine*(0.45+0.7*n1) + pkBody*pkBright*1.4;          // small + large
    float caustic = 0.5 + 0.5*sin(lx*PI*2.0 + ly*5.0 + ci_c*2.0);

    float body = (0.035 + detail) * inBand * laneEdge;               // low floor -> dark ground
    body += caustic * 0.04 * shim * inBand * laneAct;
    col += cc * body * bright * 1.35;                                // boosted contrast
    col += cc * (edgeL+edgeR) * 0.5 * eg;                            // hairline channel edges
    col += vec3(1.0) * smoothstep(0.6, 1.0, detail) * inBand * 0.55; // white-hot on packets

    // --- PTP beat: all three channels flash + a single horizontal sync line sweeps ---
    float beat = fract(uLook.w * uLook.x);
    float flash = pow(max(0.0, 1.0 - beat*4.0), 3.0);
    col *= 1.0 + 0.4*flash;
    float syncY = 1.0 - beat;                  // sweeps top->bottom each beat
    float sline = smoothstep(0.006, 0.0, abs(uv.y - syncY));
    col += vec3(0.6,0.9,1.0) * sline * uLook.y * (0.6+0.4*flash);

    fragColor = vec4(max(col, 0.0), 1.0);
}
