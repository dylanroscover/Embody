// Murmuration - speed -> dusk color ramp + point size (render side branch)
void main(){
    uint id = TDIndex();
    if(id >= TDNumElements()) return;
    vec3 V = TDIn_PartVel(0, id);
    float sp = length(V);
    float t = clamp((sp - 0.12) / 0.55, 0.0, 1.0);
    vec3 violet = vec3(0.16, 0.09, 0.55);   // slow - deep blue-violet
    vec3 cyan   = vec3(0.55, 0.92, 1.00);   // mid  - ice cyan/white
    vec3 amber  = vec3(1.00, 0.68, 0.26);   // fast - warm amber/gold
    vec3 col = (t < 0.5) ? mix(violet, cyan, t * 2.0)
                         : mix(cyan, amber, (t - 0.5) * 2.0);
    Color[id] = vec4(col * 0.7, 1.0);
    PointScale[id] = 1.0 + t * 0.9;
    P[id] = TDIn_P(0, id);
}
