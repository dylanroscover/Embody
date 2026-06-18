// Bright-pass + gaussian blur, run at 1/4 resolution (cheap, constant cost at 4K).
// Because this buffer is small, a modest tap radius covers a WIDE area of the final image.
uniform vec4 uBloom;   // x=strength, y=threshold, z=blurWidth
out vec4 fragColor;
void main(){
    vec2 uv = vUV.st;
    vec2 texel = uTDOutputInfo.res.xy;   // 1/lowW, 1/lowH (large in screen terms)
    float thr = uBloom.y;
    float bw  = uBloom.z;
    vec3 sum = vec3(0.0); float wsum = 0.0;
    const int R = 6;                     // constant; 13x13 taps but only on ~1/16 the pixels
    for(int x=-R;x<=R;x++){
        for(int y=-R;y<=R;y++){
            vec2 o = vec2(float(x), float(y));
            float w = exp(-dot(o,o)/22.0);
            vec3 s = texture(sTD2DInputs[0], uv + o*texel*bw).rgb;
            sum += max(s - thr, 0.0) * w;
            wsum += w;
        }
    }
    vec3 bloom = (sum/max(wsum,1e-4)) * uBloom.x;
    fragColor = vec4(bloom, 1.0);        // bloom-only; comp_bloom adds it back over the sharp src
}
