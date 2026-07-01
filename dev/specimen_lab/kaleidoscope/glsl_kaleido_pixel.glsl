uniform vec4 uParams;   // x=segs, y=rot_base, z=zoom_base, w=time
out vec4 fragColor;
const float PI=3.14159265;
void main(){
    float t = uParams.w;
    float aspect = uTDOutputInfo.res.z / uTDOutputInfo.res.w;   // width/height
    vec2 uv = vUV.st - 0.5;
    uv.x *= aspect;                                             // square the polar space -> no skew at any aspect ratio
    float segs = max(2.0, uParams.x);
    float rot  = uParams.y + t*0.06;
    float zoom = max(0.1, uParams.z) * (1.0 + 0.10*sin(t*0.20));
    float r = length(uv) * zoom;
    float a = atan(uv.y, uv.x) + rot;
    a += 0.35 * sin(t*0.15) * r;                 // oscillating twist (swirl)
    float seg = 2.0*PI/segs;
    a = mod(a, seg);
    a = abs(a - seg*0.5);
    vec2 pp = vec2(cos(a), sin(a)) * r + 0.5;
    pp += vec2(sin(t*0.07)*0.15 + t*0.012,        // content drifts/tumbles
               cos(t*0.05)*0.15 + t*0.009);
    pp = abs(fract(pp*0.5)*2.0 - 1.0);
    fragColor = texture(sTD2DInputs[0], pp);
}