import {afterEach,describe,expect,it,vi} from "vitest";
import {getPublicSources} from "./client";

afterEach(()=>vi.unstubAllGlobals());
describe("public source responses",()=>{
  it("preserves the 21 registered sources and their string languages",async()=>{
    const data=Array.from({length:21},(_,index)=>({source_id:`source-${index}`,name:`Fuente ${index+1}`,language:index<15?"es":"en"}));
    vi.stubGlobal("fetch",vi.fn().mockResolvedValue(new Response(JSON.stringify({data}))));
    expect(await getPublicSources()).toEqual(data.map((item)=>({sourceId:item.source_id,name:item.name,language:item.language})));
  });
  it.each([{language:["es"]},{language:7},{language:{}}])("rejects a non-string language: $language",async({language})=>{
    vi.stubGlobal("fetch",vi.fn().mockResolvedValue(new Response(JSON.stringify({data:[{source_id:"source-1",name:"Fuente",language}]}))));
    await expect(getPublicSources()).rejects.toThrow("No se pudo consultar la lista de fuentes.");
  });
});
