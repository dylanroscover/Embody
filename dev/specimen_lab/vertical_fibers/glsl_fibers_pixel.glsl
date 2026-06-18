// Vertical Fibers -- a full-height curtain of glass strands with light pulses racing UP.
// 4K-cheap: NO strand loop. Each pixel maps to its nearest strand via a periodic column-fold.
uniform vec4 uFiber;   // x=strands, y=converge, z=bow, w=glassGlow
uniform vec4 uPulse;   // x=pulseSpeed, y=pulseDensity, z=pulseLength, w=time(=absTime*Speed)
uniform vec4 uLook;    // x=haze, y=lineWidth, z=ptpRate, w=absTime
out vec4 fragColor;
const float PI = 3.14159265;

float hash11(float p){ return fract(sin(p*127.1)*43758.5453); }

// per-strand color: mostly cyan, a few green/magenta/amber essence channels
vec3 strandColor(float i){
    float k = mod(i, 7.0);
    if(k < 4.0) return vec3(0.00, 0.85, 1.00);   // cyan hero
    if(k < 5.0) return vec3(0.22, 1.00, 0.42);   // green
    if(k < 6.0) return vec3(1.00, 0.20, 0.62);   // magenta
    return vec3(1.00, 0.70, 0.10);               // amber
}

void main(){
    vec2 uv = vUV.st;                                  // 0..1, y up
    float aspect = uTDOutputInfo.res.z / uTDOutputInfo.res.w; // ~0.5625 for 9:16
    // work in a frame where x is scaled by aspect so strands are evenly spaced
    float x = (uv.x - 0.5) * aspect;
    float y = uv.y;                                    // 0 bottom .. 1 top

    vec3 col = mix(vec3(0.012,0.014,0.018), vec3(0.006,0.007,0.010), y); // charcoal, darker at top

    float nS    = clamp(uFiber.x, 4.0, 64.0);
    float conv  = uFiber.y;        // how strongly the bundle converges toward the top
    float bowA  = uFiber.z;        // gentle cable bow
    float gglow = uFiber.w;
    float lw    = uLook.y;
    float haze  = uLook.x;
    float t     = uPulse.w;
    float pdens = uPulse.y;
    float plen  = uPulse.z;

    // --- convergence: strands fan wider at the bottom, tighter toward the top (depth up) ---
    // spread() maps a normalized strand slot to an x position; it shrinks with y.
    float spreadHalf = mix(0.62*aspect, 0.62*aspect*(1.0-0.55*conv), y);

    // place strands in a periodic lattice across [-spreadHalf, spreadHalf]
    // column-fold: xn in 0..nS gives the strand index and the local offset, O(1).
    float xn   = (x + spreadHalf) / (2.0*spreadHalf) * nS;   // 0..nS across the bundle
    float idx  = floor(xn);
    float idxC = clamp(idx, 0.0, nS-1.0);
    float fi   = idxC;
    float sd   = hash11(fi*3.1);

    // strand center x as a function of y: lattice slot + a slow per-strand bow
    float slotX = (idxC + 0.5)/nS * (2.0*spreadHalf) - spreadHalf;
    float bow   = (sin(y*PI*0.85 + sd*6.28 + uLook.w*0.45)*0.011 + sin(y*PI*0.32 + sd*3.0 - uLook.w*0.27)*0.008) * bowA * (1.0 - 0.3*y);
    float cx    = slotX + bow;

    // analytic horizontal distance to THIS strand center (crisp via fwidth)
    float d  = abs(x - cx);
    float depth = sd;                                  // per-strand pseudo-depth 0..1
    float w  = lw * mix(1.1, 0.92, y) * mix(1.15, 0.9, depth); // thinner toward top + far strands
    float aa = fwidth(x) * 1.5;                        // resolution-independent edge
    float tube = smoothstep(w+aa, w-aa, d);            // crisp 1px-ish core at any res
    float refl = smoothstep(w*3.0, 0.0, d) * (0.82 + 0.18*sin(y*7.0 + sd*5.0 + t*1.2));
    float dim  = mix(1.0, 0.35, y);                    // far (top) strands dim into haze

    vec3 sc = strandColor(fi);
    col += sc * (tube*0.95*gglow + refl*0.32*gglow) * mix(1.0, 0.88, y);

    // --- traveling light pulses: bright Gaussian dashes racing UP this strand, wrapping ---
    float onStrand = smoothstep(w*2.5, 0.0, d);
    float speedJ = 0.6 + 0.6*hash11(fi*4.2);
    const int MAXP = 4;                                // CONSTANT bound
    for(int k=0;k<MAXP;k++){
        if(float(k) >= pdens) break;
        float fk = float(k);
        float phase = fract(hash11(fi*9.0+fk*2.3) + t*speedJ);
        float along = y - phase;                       // travel up (y increases)
        float pulse = exp(-pow(along/plen, 2.0)*7.0) * onStrand;
        vec3 core = mix(sc, vec3(1.0), 0.6);           // white-hot core
        col += core * pulse * dim * 0.8;
    }

    // PTP beat: all pulses breathe brighter in lockstep
    float beat = fract(uLook.w * uLook.z);
    float flash = pow(max(0.0, 1.0 - beat*4.0), 3.0);
    col *= 1.0 + 0.35*flash;

    // atmospheric haze building toward the top (the vanishing direction)
    col += vec3(0.018,0.026,0.034) * haze * smoothstep(0.4, 1.0, y);

    fragColor = vec4(max(col, 0.0), 1.0);
}
