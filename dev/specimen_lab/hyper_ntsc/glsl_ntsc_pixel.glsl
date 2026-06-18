// HYPER NTSC -- neon cyberpunk source through heavy NTSC artifacts:
// YIQ chroma bleed, dot crawl (subcarrier), scanlines, analog snow, sync jitter.
uniform vec4 uNtsc;   // x=bleed, y=dotcrawl, z=noise, w=jitter
uniform vec4 uScan;   // x=scanlines, y=bandcount, z=hue, w=speed
uniform vec4 uTime;   // x=absTime
out vec4 fragColor;
const float PI = 3.14159265;
float h11(float p){ return fract(sin(p*127.1)*43758.5453); }
float h21(vec2 p){ return fract(sin(dot(p, vec2(127.1,311.7)))*43758.5453); }
vec3 rgb2yiq(vec3 c){ return vec3(dot(c,vec3(0.299,0.587,0.114)), dot(c,vec3(0.596,-0.274,-0.322)), dot(c,vec3(0.211,-0.523,0.312))); }
vec3 yiq2rgb(vec3 v){ return vec3(v.x+0.956*v.y+0.621*v.z, v.x-0.272*v.y-0.647*v.z, v.x-1.106*v.y+1.703*v.z); }
vec3 neon(float s, float hue){
    float h = fract(s + hue);
    vec3 mag=vec3(1.00,0.10,0.70), cyn=vec3(0.10,0.95,1.00), vio=vec3(0.55,0.20,1.00);
    return (h<0.33)? mix(mag,cyn,h*3.0) : (h<0.66)? mix(cyn,vio,(h-0.33)*3.0) : mix(vio,mag,(h-0.66)*3.03);
}
vec3 source(vec2 uv, float t, float bands, float hue){
    float bi = floor(uv.y * bands);
    float seed = h11(bi*1.3 + floor(t*0.25));
    vec3 col = neon(seed, hue);
    float wave = 0.45 + 0.55*sin(uv.x*7.0 + t*1.6 + bi*2.1);
    col *= (0.30 + 0.70*wave);
    float scanbar = smoothstep(0.04, 0.0, abs(fract(uv.x*0.5 - t*0.08) - 0.5));
    col += neon(seed+0.5, hue) * scanbar * 0.6;
    vec2 g = abs(fract(uv*vec2(28.0, bands)) - 0.5);
    col *= 0.55 + 0.45*smoothstep(0.45, 0.49, max(g.x, g.y));
    return col;
}
void main(){
    vec2 uv = vUV.st;
    vec4 res = uTDOutputInfo.res;
    float t = uTime.x * uScan.w;
    float bands = uScan.y, hue = uScan.z;
    float row = floor(uv.y * res.w);
    float rj  = h11(row*0.07 + floor(t*12.0));
    float jit = (rj-0.5)*0.004*uNtsc.w;
    float tear = step(0.992, h11(floor(t*3.0)*1.3)) * step(0.7, rj) * 0.05 * uNtsc.w;
    vec2 suv = uv + vec2(jit+tear, 0.0);
    vec3 base = rgb2yiq(source(suv, t, bands, hue));
    vec2 iq = vec2(0.0); float wsum = 0.0;
    for(int k=-4;k<=4;k++){
        float o = float(k)*uNtsc.x*0.004;
        vec3 s = rgb2yiq(source(suv+vec2(o,0.0), t, bands, hue));
        float w = 1.0 - abs(float(k))*0.18;
        iq += s.yz*w; wsum += w;
    }
    iq /= wsum;
    float sub = uv.x*res.z*0.4 + uv.y*res.w*0.5 + t*5.0;
    float Y = base.x + sin(sub)*length(iq)*uNtsc.y*0.18;
    vec3 rgb = yiq2rgb(vec3(Y, iq));
    float sl = 0.82 + 0.18*sin(uv.y*res.w*PI);
    rgb *= mix(1.0, sl, uScan.x);
    rgb += (h21(uv*res.zw + t*50.0)-0.5)*uNtsc.z*0.18;
    fragColor = vec4(max(rgb,0.0), 1.0);
}