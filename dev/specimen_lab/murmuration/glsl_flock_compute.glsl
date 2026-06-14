// Murmuration - GPU flocking compute (GLSL POP), TRUE per-neighbor Reynolds.
// Iterates the Neighbor POP index list (Nebr) to compute cohesion, alignment,
// and inverse-square separation directly - the separation is dominated by the
// closest neighbor so it never cancels in a symmetric clump (even spacing).
//   uWeights   = (cohesion, alignment, separation, attractor)
//   uDynamics  = (bodyRadius, maxForce, curl, time)
//   uAttractor = (ax, ay, az, spiral)

vec3 hash33(vec3 p){
    p = vec3(dot(p, vec3(127.1,311.7,74.7)), dot(p, vec3(269.5,183.3,246.1)), dot(p, vec3(113.5,271.9,124.6)));
    return fract(sin(p) * 43758.5453) * 2.0 - 1.0;
}
float vnoise(vec3 p){
    vec3 i=floor(p); vec3 f=fract(p); vec3 u=f*f*(3.0-2.0*f);
    return mix(mix(mix(dot(hash33(i+vec3(0,0,0)),f-vec3(0,0,0)), dot(hash33(i+vec3(1,0,0)),f-vec3(1,0,0)),u.x),
                   mix(dot(hash33(i+vec3(0,1,0)),f-vec3(0,1,0)), dot(hash33(i+vec3(1,1,0)),f-vec3(1,1,0)),u.x),u.y),
               mix(mix(dot(hash33(i+vec3(0,0,1)),f-vec3(0,0,1)), dot(hash33(i+vec3(1,0,1)),f-vec3(1,0,1)),u.x),
                   mix(dot(hash33(i+vec3(0,1,1)),f-vec3(0,1,1)), dot(hash33(i+vec3(1,1,1)),f-vec3(1,1,1)),u.x),u.y),u.z);
}
vec3 snoiseVec3(vec3 x){ return vec3(vnoise(x), vnoise(x+vec3(123.4,0,0)), vnoise(x+vec3(0,234.5,0))); }
vec3 curlNoise(vec3 p){
    const float e=0.1; vec3 dx=vec3(e,0,0), dy=vec3(0,e,0), dz=vec3(0,0,e);
    vec3 px0=snoiseVec3(p-dx),px1=snoiseVec3(p+dx),py0=snoiseVec3(p-dy),py1=snoiseVec3(p+dy),pz0=snoiseVec3(p-dz),pz1=snoiseVec3(p+dz);
    float x=(py1.z-py0.z)-(pz1.y-pz0.y), y=(pz1.x-pz0.x)-(px1.z-px0.z), z=(px1.y-px0.y)-(py1.x-py0.x);
    return normalize(vec3(x,y,z)/(2.0*e) + 1e-6);
}

void main(){
    uint id = TDIndex();
    if(id >= TDNumElements()) return;

    vec3 P0 = TDIn_P(0, id);
    vec3 V0 = TDIn_PartVel(0, id);
    int nN = int(TDIn_NumNebrs(0, id));

    vec3 sumPos = vec3(0.0);
    vec3 sumVel = vec3(0.0);
    vec3 sep = vec3(0.0);
    int cnt = 0;
    const int MAXN = 16;
    for(int i = 0; i < MAXN; i++){
        if(i >= nN) break;
        uint nIdx = TDIn_Nebr(0, id, i);
        vec3 nP = TDIn_P(0, uint(nIdx));
        vec3 nV = TDIn_PartVel(0, uint(nIdx));
        vec3 diff = P0 - nP;
        float dist = length(diff) + 1e-5;
        sumPos += nP;
        sumVel += nV;
        float push = clamp(0.03 / (dist*dist), 0.0, 6.0); // inverse-square-capped: closest neighbor
        sep += (diff / dist) * push;                     // closest neighbor dominates (no cancel)
        cnt++;
    }

    vec3 force = vec3(0.0);
    if(cnt > 0){
        vec3 nbrCenter = sumPos / float(cnt);
        vec3 avgVel   = sumVel / float(cnt);
        force += uWeights.x * (nbrCenter - P0);     // cohesion
        force += uWeights.y * (avgVel - V0);       // alignment
        force += uWeights.z * sep;                 // separation: summed per-neighbor push (crowded => stronger)
    }

    // moving attractor - slow radial pull + tangential spiral
    vec3 toA = uAttractor.xyz - P0;
    float dA = length(toA) + 1e-5;
    vec3 dirA = toA / dA;
    force += uWeights.w * dirA;
    vec3 tang = normalize(cross(vec3(0.0,1.0,0.0), dirA) + 1e-6);
    force += uAttractor.w * tang;

    // curl wander
    vec3 cp = P0 * 1.6 + vec3(0.0, uDynamics.w, 0.0);
    force += uDynamics.z * curlNoise(cp);

    // clamp the steering forces
    float maxF = uDynamics.y;
    float fl = length(force);
    if(fl > maxF) force *= maxF / fl;

    // containment + drag AFTER the clamp
    float R = uDynamics.x;
    if(dA > R) force += dirA * (dA - R) * 1.5;
    force -= V0 * 0.7;

    PartForce[id] = force;
    P[id] = P0;
}
