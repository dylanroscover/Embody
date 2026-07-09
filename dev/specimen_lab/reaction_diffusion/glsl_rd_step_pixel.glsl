out vec4 fragColor;
const float feed = 0.055;
const float kill = 0.062;
const float da = 1.0;
const float db = 0.5;
void main(){
    vec2 texel = uTD2DInfos[0].res.xy;
    vec2 uv = vUV.st;
    vec2 s = texture(sTD2DInputs[0], uv).rg;
    float a = s.r; float b = s.g;
    vec2 lap = vec2(0.0);
    lap += texture(sTD2DInputs[0], uv + texel*vec2(-1.0,-1.0)).rg * 0.05;
    lap += texture(sTD2DInputs[0], uv + texel*vec2( 0.0,-1.0)).rg * 0.20;
    lap += texture(sTD2DInputs[0], uv + texel*vec2( 1.0,-1.0)).rg * 0.05;
    lap += texture(sTD2DInputs[0], uv + texel*vec2(-1.0, 0.0)).rg * 0.20;
    lap += s * -1.0;
    lap += texture(sTD2DInputs[0], uv + texel*vec2( 1.0, 0.0)).rg * 0.20;
    lap += texture(sTD2DInputs[0], uv + texel*vec2(-1.0, 1.0)).rg * 0.05;
    lap += texture(sTD2DInputs[0], uv + texel*vec2( 0.0, 1.0)).rg * 0.20;
    lap += texture(sTD2DInputs[0], uv + texel*vec2( 1.0, 1.0)).rg * 0.05;
    float reaction = a*b*b;
    float na = a + (da*lap.r - reaction + feed*(1.0-a));
    float nb = b + (db*lap.g + reaction - (kill+feed)*b);
    fragColor = vec4(clamp(na,0.0,1.0), clamp(nb,0.0,1.0), 0.0, 1.0);
}
