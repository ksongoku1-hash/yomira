import {QueryClient, QueryClientProvider} from "@tanstack/react-query";
import {fireEvent, render, screen, waitFor, within} from "@testing-library/react";
import {MemoryRouter} from "react-router-dom";
import {beforeEach, describe, expect, it, vi} from "vitest";
import * as api from "../api/client";
import {OnlineRecommendations} from "./OnlineRecommendations";

vi.mock("../api/client",()=>({getRecommendations:vi.fn()}));
const feed: api.RecommendationFeed = {data:[
  {remoteId:"123e4567-e89b-42d3-a456-426614174001",title:"The Boxer",aliases:["El boxeador"],genres:["Action"],coverUrl:null},
  {remoteId:"123e4567-e89b-42d3-a456-426614174002",title:"Solo Leveling",aliases:["Na Honjaman Level-Up"],genres:["Action","Fantasy"],coverUrl:"https://uploads.mangadex.org/covers/123e4567-e89b-42d3-a456-426614174002/123e4567-e89b-42d3-a456-426614174003.jpg"},
  {remoteId:"123e4567-e89b-42d3-a456-426614174004",title:"Misterio & aventura",aliases:[],genres:["Mystery"],coverUrl:null},
],meta:{provider:"MangaDex",status:"ready",updatedAt:"2026-10-21T12:00:00Z",retryAt:null}};
function open(children:React.ReactNode) {
  const client=new QueryClient({defaultOptions:{queries:{retry:false,gcTime:0}}});
  return render(<QueryClientProvider client={client}><MemoryRouter>{children}</MemoryRouter></QueryClientProvider>);
}
beforeEach(()=>vi.mocked(api.getRecommendations).mockReset().mockResolvedValue(feed));
describe("online recommendations",()=>{
  it("shares one ranking between sections and sends the reader to search without importing anything",async()=>{
    open(<><OnlineRecommendations kind="popular"/><OnlineRecommendations kind="personal" readingTitles={["El boxeador"]} readingGenres={["Acción"]}/></>);
    const popular=screen.getByRole("region",{name:"Populares online"});
    const personal=screen.getByRole("region",{name:"Descubre algo nuevo"});
    expect(await within(popular).findByRole("link",{name:"Solo Leveling"})).toHaveAttribute("href","/search?q=Solo%20Leveling");
    expect(api.getRecommendations).toHaveBeenCalledTimes(1);
    expect(within(popular).getByText("#2")).toBeInTheDocument();
    expect(within(personal).queryByRole("link",{name:"The Boxer"})).not.toBeInTheDocument();
    expect(within(personal).getByText("Porque lees Acción")).toBeInTheDocument();
    expect(within(popular).getByRole("link",{name:"Misterio & aventura"})).toHaveAttribute("href","/search?q=Misterio%20%26%20aventura");
    expect(screen.queryByText(/En 3 fuentes|Disponible para leer/)).not.toBeInTheDocument();
  });
  it("filters locally and offers alternate title searches",async()=>{
    open(<OnlineRecommendations kind="personal"/>);
    await screen.findByRole("link",{name:"Solo Leveling"});
    const calls=vi.mocked(api.getRecommendations).mock.calls.length;
    fireEvent.click(screen.getByRole("button",{name:"Misterio"}));
    expect(screen.queryByRole("link",{name:"Solo Leveling"})).not.toBeInTheDocument();
    expect(screen.getByRole("link",{name:"Misterio & aventura"})).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button",{name:"Todos"}));
    fireEvent.click(screen.getAllByText("Otros nombres")[1]!);
    expect(screen.getByRole("link",{name:"Na Honjaman Level-Up"})).toHaveAttribute("href","/search?q=Na%20Honjaman%20Level-Up");
    expect(api.getRecommendations).toHaveBeenCalledTimes(calls);
  });
  it("keeps a stale ranking visible and explains a failed initial request",async()=>{
    vi.mocked(api.getRecommendations).mockResolvedValueOnce({...feed,meta:{...feed.meta,status:"stale"}});
    const view=open(<OnlineRecommendations kind="popular"/>);
    expect(await screen.findByText(/última lista guardada/)).toBeInTheDocument();
    expect(screen.getByRole("link",{name:"Solo Leveling"})).toBeInTheDocument();
    view.unmount();
    vi.mocked(api.getRecommendations).mockRejectedValueOnce(new Error("offline"));
    open(<OnlineRecommendations kind="popular"/>);
    await screen.findByText("No se pudieron consultar las recomendaciones");
    fireEvent.click(screen.getByRole("button",{name:"Comprobar recomendaciones"}));
    await screen.findByRole("link",{name:"Solo Leveling"});
  });
  it("falls back after a failed cover while preserving the search action",async()=>{
    open(<OnlineRecommendations kind="popular"/>);
    const image=await screen.findByRole("img",{name:"Portada de Solo Leveling"});
    expect(image).toHaveAttribute("loading","lazy");
    expect(image.getAttribute("src")).toMatch(/\.jpg\.256\.jpg$/);
    fireEvent.error(image);
    expect(screen.getByRole("img",{name:"Portada no disponible: Solo Leveling"})).toBeInTheDocument();
    expect(screen.getByRole("link",{name:"Buscar fuentes de Solo Leveling"})).toHaveAttribute("href","/search?q=Solo%20Leveling");
  });
  it("waits for the local service before fetching discovery data",async()=>{
    const view=open(<OnlineRecommendations kind="popular" enabled={false}/>);
    expect(screen.getByText(/cuando el servicio local esté listo/)).toBeInTheDocument();
    await waitFor(()=>expect(api.getRecommendations).not.toHaveBeenCalled());
    view.unmount();
  });
});


it("switches catalogs and expands cached recommendations without importing works", async()=>{
  vi.mocked(api.getRecommendations).mockImplementation(async (provider)=>({...feed,
    data:Array.from({length:24},(_,i)=>({...feed.data[0]!,remoteId:`anilist:${i+1}`,title:`Obra ${i+1}`,aliases:[],coverUrl:null})),
    meta:{...feed.meta,provider:provider??"MangaDex"}}));
  open(<OnlineRecommendations kind="popular"/>);
  await screen.findByRole("link",{name:"Obra 1"});
  fireEvent.change(screen.getByRole("combobox",{name:"Catálogo de Populares online"}),{target:{value:"AniList"}});
  await waitFor(()=>expect(api.getRecommendations).toHaveBeenCalledWith("AniList"));
  await screen.findByText("#1");
  expect(screen.queryByRole("link",{name:"Obra 13"})).not.toBeInTheDocument();
  const calls=vi.mocked(api.getRecommendations).mock.calls.length;
  fireEvent.click(screen.getByRole("button",{name:"Ver más recomendaciones"}));
  expect(screen.getByRole("link",{name:"Obra 13"})).toHaveAttribute("href","/search?q=Obra%2013");
  expect(api.getRecommendations).toHaveBeenCalledTimes(calls);
});
