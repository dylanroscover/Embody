// Cheap O(1) finish: crisp gutter rule + grain + vignette. No blur loops.
uniform vec4 uFin;   // x=grain, y=vignette, z=streams, w=absTime
out vec4 fragColor;
float hash21(vec2 p){ return fract(sin(dot(p, vec2(127.1,311.7)))*43758.5453); }
void main(){
    vec2 uv = vUV.st;
    vec3 col = texture(sTD2DInputs[0], uv).rgb;
    float nC = clamp(uFin.z, 1.0, 3.0);
    // re-assert crisp hairline gutters between channels at full res
    float lx = fract(uv.x * nC);
    float aa = fwidth(lx);
    float rule = smoothstep(aa*2.0, 0.0, min(lx, 1.0-lx));
    col *= 1.0 - 0.5*rule;          // thin dark separator
    // vignette
    vec2 p = uv - 0.5;
    float vig = 1.0 - uFin.y * smoothstep(0.35, 0.85, length(vec2(p.x*0.6, p.y)));
    col *= vig;
    // grain
    col += (hash21(uv*uTDOutputInfo.res.zw + uFin.w*60.0)-0.5) * 0.012 * uFin.x;
    fragColor = vec4(col, 1.0);
}
