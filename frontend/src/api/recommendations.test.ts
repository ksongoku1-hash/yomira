import {afterEach,describe,expect,it,vi} from "vitest";
import {getRecommendations} from "./client";

function payload() { return {data:[{remote_id:"123e4567-e89b-42d3-a456-426614174001",title:"Solo Leveling",aliases:["Na Honjaman Level-Up"],genres:["Action"],cover_url:null}],
  meta:{provider:"MangaDex",criterion:"followed_count",status:"ready",updated_at:"2026-10-21T12:00:00Z",retry_at:null}}; }
afterEach(()=>vi.unstubAllGlobals());
describe("recommendation responses",()=>{
  it("reads only public discovery metadata without sending reading history",async()=>{
    const fetchMock=vi.fn().mockResolvedValue(new Response(JSON.stringify(payload())));
    vi.stubGlobal("fetch",fetchMock);
    expect((await getRecommendations()).data[0]?.title).toBe("Solo Leveling");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/v1/recommendations");
    expect(fetchMock.mock.calls[0]?.[1].body).toBeUndefined();
  });
  it.each([{status:["ready"]},{status:7},{status:{}}])("rejects a non-string status: $status",async({status})=>{
    const data = {...payload(),meta:{...payload().meta,status}};
    vi.stubGlobal("fetch",vi.fn().mockResolvedValue(new Response(JSON.stringify(data))));
    await expect(getRecommendations()).rejects.toThrow("No se pudo leer la lista de recomendaciones.");
  });
  it.each(["duplicate","invalid-id","bad-cover","long-title","bad-date","bad-status"])("rejects %s",async(kind)=>{
    const value:unknown=payload();
    const data=value as {data:Record<string,unknown>[];meta:Record<string,unknown>};
    if(kind==="duplicate") data.data.push({...data.data[0]});
    else if(kind==="invalid-id") data.data[0]!.remote_id="../private";
    else if(kind==="bad-cover") data.data[0]!.cover_url="https://attacker.example/cover.jpg";
    else if(kind==="long-title") data.data[0]!.title="x".repeat(201);
    else if(kind==="bad-date") data.meta.updated_at="bad date";
    else data.meta.status="unavailable";
    vi.stubGlobal("fetch",vi.fn().mockResolvedValue(new Response(JSON.stringify(data))));
    await expect(getRecommendations()).rejects.toThrow("No se pudo leer la lista de recomendaciones.");
  });
});


it.each(["AniList","MyAnimeList"] as const)("validates the %s provider contract and image host",async(provider)=>{
  const data={data:[{...payload().data[0],remote_id:provider==="AniList"?"anilist:12":"jikan:12",
    cover_url:provider==="AniList"?"https://s4.anilist.co/file/anilistcdn/media/manga/cover/large/a.jpg":"https://cdn.myanimelist.net/images/manga/1/2.jpg"}],
    meta:{...payload().meta,provider,criterion:provider==="AniList"?"trending":"popularity"}};
  vi.stubGlobal("fetch",vi.fn().mockImplementation(async()=>new Response(JSON.stringify(data))));
  expect((await getRecommendations(provider)).data[0]?.title).toBe("Solo Leveling");
  data.data[0]!.cover_url="https://attacker.example/a.jpg";
  await expect(getRecommendations(provider)).rejects.toThrow();
});
