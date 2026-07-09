uniform vec4 uParams;   // x=scale, y=warp, z=complexity(detune), w=time(=absTime*Speed)
uniform vec4 uPalette;  // x=hue 0..1, y=contrast
out vec4 fragColor;
const float PI = 3.14159265;

// Inigo-Quilez cyclic cosine palette: smooth, loops in hue, never bands.
vec3 palette(float t, float hue, float contrast){
    vec3 a = vec3(0.5);
    vec3 b = vec3(0.5) * contrast;
    vec3 c = vec3(1.0, 1.0, 1.0);
    vec3 d = vec3(0.00, 0.33, 0.67) + hue;   // hue rotates the phase offsets
    return a + b * cos(6.28318 * (c * t + d));
}

// One animated sine field: a handful of moving plane-waves summed.
float field(vec2 p, float time, float freq){
    float v = 0.0;
    v += sin(p.x * freq + time);
    v += sin(p.y * freq * 1.3 + time * 1.1);
    v += sin((p.x + p.y) * freq * 0.7 + time * 0.9);
    float cx = p.x + 0.5 * sin(time * 0.4);
    float cy = p.y + 0.5 * cos(time * 0.35);
    v += sin(sqrt(cx * cx + cy * cy) * freq * 1.6 + time * 1.3);  // radial ripple, moving center
    return v;
}

void main(){
    float aspect = uTDOutputInfo.res.z / uTDOutputInfo.res.w;  // output res = (1/w,1/h,w,h); zw = pixel dims
    vec2 uv = vUV.st;
    vec2 p  = (uv - 0.5);
    p.x *= aspect;                               // keep the plasma square on wide formats

    float t      = uParams.w;
    float scale  = max(0.5, uParams.x);
    float warp   = uParams.y;
    float detune = uParams.z;

    p *= scale;

    // Domain warp: a slow rotating swirl bends the sample coords -> liquid flow.
    vec2 w = vec2(
        sin(p.y * 1.7 + t * 0.5),
        cos(p.x * 1.5 - t * 0.45)
    );
    vec2 pw = p + warp * w;

    // Two fields at slightly detuned scales BEAT against each other -> fringes.
    float f1 = field(pw, t, 1.0);
    float f2 = field(pw, t * 1.05, 1.0 + 0.25 * detune);
    float v  = (f1 + f2) * 0.25;                 // ~[-2,2]; folded cyclically into 0..1 below
    v += 0.30 * detune * sin((f1 - f2) * PI);    // explicit beat term -> visible moire shimmer

    float tone = 0.5 + 0.5 * sin(v * PI);        // fold into 0..1, smooth + cyclic

    vec3 col = palette(tone, uPalette.x, 0.6 + uPalette.y);

    float d = length(uv - 0.5);                  // gentle vignette: reads as a framed loop
    col *= mix(1.0, 0.55, smoothstep(0.45, 0.95, d));

    fragColor = vec4(clamp(col, 0.0, 1.0), 1.0);
}
