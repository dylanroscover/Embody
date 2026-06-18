// Bright-pass + gaussian blur at 1/4 resolution. The 4K-safe replacement for a full-res tap loop.
uniform vec4 uBloom;   // x=strength, y=threshold, z=blurWidth
out vec4 fragColor;
void main(){
    vec2 uv = vUV.st;
    vec2 texel = uTDOutputInfo.res.xy;   // 1/lowW, 1/lowH (large in screen terms)
    float thr = uBloom.y;
    float bw  = uBloom.z;
    vec3 sum = vec3(0.0); float wsum = 0.0;
    const int R = 6;                     // 13x13 taps but only on ~1/16 the pixels
    for(int x=-R;x<=R;x++){
        for(int y=-R;y<=R;y++){
            vec2 o = vec2(float(x), float(y));
            float w = exp(-dot(o,o)/22.0);
            vec3 s = texture(sTD2DInputs[0], uv + o*texel*bw).rgb;
            sum += max(s - thr, 0.0) * w;
            wsum += w;
        }
    }
    fragColor = vec4((sum/max(wsum,1e-4)) * uBloom.x, 1.0);
}
