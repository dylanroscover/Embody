out vec3 iWorldPos;
out vec3 iWorldNorm;
void main(){
    vec3 pos = TDPos();
    vec4 worldSpacePos = TDDeform(pos);
    iWorldPos = worldSpacePos.xyz;
    iWorldNorm = TDDeformNorm(TDNormal());
    gl_Position = TDWorldToProj(worldSpacePos);
}