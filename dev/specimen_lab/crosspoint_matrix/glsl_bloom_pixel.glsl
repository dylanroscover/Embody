// VHS finish: chroma split + rolling scanlines + tape jitter/tear + grain.
// Bloom is now a separate downsampled chain composited upstream (cheap at 4K).
uniform vec4 uBloom;   // z=glitch, w=time
out vec4 fragColor;
float hash21(vec2 p){ return fract(sin(dot(p, vec2(127.1,311.7)))*43758.5453); }
float hash11(float p){ return fract(sin(p*78.233)*43758.5453); }
void main(){
    vec2 uv = vUV.st;
    float gl = uBloom.z;
    float tm = uBloom.w;
    float row = floor(uv.y * uTDOutputInfo.res.w);
    float lrand = hash11(row*0.13 + floor(tm*12.0));
    float jitter = (lrand - 0.5) * 0.006 * gl;
    float tearZone = step(0.955, hash11(floor(tm*4.0)*1.7));
    float tear = tearZone * step(0.6, lrand) * (lrand-0.6) * 0.07 * gl;
    float xoff = jitter + tear;
    float ca = (0.0032 + tear*1.8) * (0.6 + 0.9*gl);
    vec3 col = vec3(
        texture(sTD2DInputs[0], uv + vec2(xoff + ca, 0.0)).r,
        texture(sTD2DInputs[0], uv + vec2(xoff,      0.0)).g,
        texture(sTD2DInputs[0], uv + vec2(xoff - ca, 0.0)).b);
    float sl = 0.80 + 0.20*sin((uv.y*uTDOutputInfo.res.w - tm*40.0) * 3.14159);
    col *= mix(1.0, sl, 0.35 + 0.4*gl);
    col += smoothstep(0.05, 0.0, abs(fract(uv.y + tm*0.13) - 0.5)) * 0.05 * gl;
    col += (hash21(uv*uTDOutputInfo.res.zw + tm)-0.5) * (0.016 + 0.03*gl);
    float lum = dot(col, vec3(0.299,0.587,0.114));
    col = mix(col, vec3(lum), 0.08*gl);
    fragColor = vec4(max(col,0.0), 1.0);
}
