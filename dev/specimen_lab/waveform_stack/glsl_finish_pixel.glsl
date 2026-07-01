// Composite-add the 1/4-res bloom over the sharp scope + scanline + grain + PTP RGB-split.
// in0 = sharp comp_scope, in1 = upscaled bloom. No blur loop here.
uniform vec4 uFin;   // x=scanlines, y=rgbSplit, z=lanes, w=ptpTime
out vec4 fragColor;
float hash21(vec2 p){ return fract(sin(dot(p, vec2(127.1,311.7)))*43758.5453); }
void main(){
    vec2 uv = vUV.st;
    float beat = fract(uFin.w);
    float ev = pow(max(0.0, 1.0 - beat*6.0), 4.0);     // brief event on the tick
    float sp = uFin.y * (0.0015 + 0.004*ev);
    // sharp source with a PTP-pulsed chroma separation
    vec3 src;
    src.r = texture(sTD2DInputs[0], uv + vec2(sp,0.0)).r;
    src.g = texture(sTD2DInputs[0], uv).g;
    src.b = texture(sTD2DInputs[0], uv - vec2(sp,0.0)).b;
    vec3 bloom = texture(sTD2DInputs[1], uv).rgb;       // upscaled 1/4-res glow
    vec3 col = src + bloom;
    // CRT scanline overlay (horizontal lines across the tall frame)
    float lc = clamp(uFin.z*48.0, 60.0, 720.0);
    float scan = 1.0 - uFin.x * 0.30 * (0.5 + 0.5*sin(uv.y * lc * 6.28318));
    col *= scan;
    col += (hash21(uv*uTDOutputInfo.res.zw + beat*97.0)-0.5) * 0.014;
    fragColor = vec4(col, 1.0);
}
