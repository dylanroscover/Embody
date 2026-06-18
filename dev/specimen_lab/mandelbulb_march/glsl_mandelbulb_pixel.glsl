// Mandelbulb March -- raymarched 3D Mandelbulb, orbit-trap color, soft light.
// uParams: x=Power  y=Detail  z=Glow  w=time(=absTime*OrbitSpeed)
// uLight : xyz=sun unit dir (from azim/elev)  w=Hue
uniform vec4 uParams;
uniform vec4 uLight;
out vec4 fragColor;

const int   DE_ITER     = 12;   // bounded fractal iterations
const int   MARCH_STEPS = 96;   // bounded raymarch steps
const int   SHADOW_STEPS = 28;  // bounded soft-shadow march
const float MAX_DIST = 8.0;

vec4 g_trap;   // orbit-trap accumulator (set in DE, read for color)

float mandelbulbDE(vec3 pos){
    vec3 z = pos;
    float dr = 1.0;
    float r  = 0.0;
    float power = uParams.x;
    vec4  trap = vec4(1e10);
    for(int i = 0; i < DE_ITER; i++){
        r = length(z);
        if(r > 2.0) break;
        trap = min(trap, vec4(abs(z), r));
        float theta = acos(clamp(z.z / r, -1.0, 1.0));
        float phi   = atan(z.y, z.x);
        dr = pow(r, power - 1.0) * power * dr + 1.0;
        float zr = pow(r, power);
        theta *= power;
        phi   *= power;
        z = zr * vec3(sin(theta) * cos(phi),
                      sin(theta) * sin(phi),
                      cos(theta));
        z += pos;
    }
    g_trap = trap;
    return 0.5 * log(r) * r / dr;
}

vec3 calcNormal(vec3 p, float eps){
    vec2 e = vec2(eps, 0.0);
    return normalize(vec3(
        mandelbulbDE(p + e.xyy) - mandelbulbDE(p - e.xyy),
        mandelbulbDE(p + e.yxy) - mandelbulbDE(p - e.yxy),
        mandelbulbDE(p + e.yyx) - mandelbulbDE(p - e.yyx)));
}

float softShadow(vec3 ro, vec3 rd, float k){
    float res = 1.0;
    float t = 0.02;
    for(int i = 0; i < SHADOW_STEPS; i++){
        vec3 p = ro + rd * t;
        float h = mandelbulbDE(p);
        if(h < 0.0008) return 0.0;
        res = min(res, k * h / t);
        t += clamp(h, 0.01, 0.2);
        if(t > 4.0) break;
    }
    return clamp(res, 0.0, 1.0);
}

vec3 palette(float h){
    return 0.5 + 0.5 * cos(6.28318 * (vec3(0.0, 0.33, 0.67) + h));
}

void main(){
    vec2 res = uTDOutputInfo.res.zw;          // output pixel resolution (no inputs -> not uTD2DInfos)
    vec2 uv  = (gl_FragCoord.xy - 0.5 * res) / res.y;

    float t = uParams.w;

    float ca = t * 0.25;
    float cam_r = 4.5;                         // hero distance: whole bulb in frame w/ generous negative space
    vec3 ro = vec3(sin(ca) * cam_r, 0.55 * sin(t * 0.13), cos(ca) * cam_r);
    vec3 ta = vec3(0.0);
    vec3 ww = normalize(ta - ro);
    vec3 uu = normalize(cross(ww, vec3(0.0, 1.0, 0.0)));
    vec3 vv = cross(uu, ww);
    float fov = 1.4;
    vec3 rd = normalize(uv.x * uu + uv.y * vv + fov * ww);

    float detail = mix(0.0015, 0.0004, clamp(uParams.y, 0.0, 1.0));
    float tDist = 0.0;
    float glowAcc = 0.0;
    bool  hit = false;
    vec4  trap = vec4(1e10);
    for(int i = 0; i < MARCH_STEPS; i++){
        vec3 p = ro + rd * tDist;
        float d = mandelbulbDE(p);
        glowAcc += exp(-d * 28.0);
        if(d < detail * (1.0 + tDist)){
            hit  = true;
            trap = g_trap;
            break;
        }
        tDist += d;
        if(tDist > MAX_DIST) break;
    }

    vec3 col = vec3(0.0);
    if(hit){
        vec3 p = ro + rd * tDist;
        vec3 n = calcNormal(p, detail * 1.5);
        vec3 sun = normalize(uLight.xyz);

        float dif = clamp(dot(n, sun), 0.0, 1.0);
        float sh  = softShadow(p + n * 0.002, sun, 16.0);
        float sky = 0.5 + 0.5 * n.y;
        float ao = clamp(1.0 - (tDist - 2.0) * 0.15, 0.3, 1.0);

        float hue = uLight.w + trap.w * 0.18 + trap.x * 0.25;
        vec3 base = palette(hue);
        base = mix(base, base.zxy, clamp(trap.y * 0.6, 0.0, 1.0));

        col  = base * (0.18 * sky);
        col += base * dif * sh * vec3(1.0, 0.93, 0.82);
        float fres = pow(1.0 - clamp(dot(n, -rd), 0.0, 1.0), 3.0);
        col += fres * 0.25 * palette(hue + 0.5);
        col *= ao;
    } else {
        vec3 sun = normalize(uLight.xyz);
        float g = 0.5 + 0.5 * rd.y;
        col = mix(vec3(0.015, 0.018, 0.03), vec3(0.04, 0.05, 0.08), g);
        col += pow(clamp(dot(rd, sun), 0.0, 1.0), 8.0) * 0.06 * palette(uLight.w + 0.5);
    }

    col += palette(uLight.w + 0.15) * glowAcc * (0.012 * uParams.z);

    col = col / (1.0 + col);
    col = pow(clamp(col, 0.0, 1.0), vec3(0.4545));
    fragColor = vec4(col, 1.0);
}
