import {QueryClient,QueryClientProvider} from "@tanstack/react-query";
import {fireEvent,render,screen} from "@testing-library/react";
import {beforeEach,describe,expect,it,vi} from "vitest";
import * as api from "../api/client";
import {AvailableSources} from "./AvailableSources";
vi.mock("../api/client",()=>({getPublicSources:vi.fn()}));
beforeEach(()=>vi.mocked(api.getPublicSources).mockReset().mockResolvedValue([
  {sourceId:"leer",name:"LeerCapitulo",language:"es"},{sourceId:"oni",name:"MangaOni",language:"es"},
]));
function open() { const client=new QueryClient({defaultOptions:{queries:{retry:false,gcTime:0}}});
  return render(<QueryClientProvider client={client}><AvailableSources/></QueryClientProvider>); }
describe("available reading sources",()=>{
  it("shows an expanded inventory without hiding additional Spanish or English sources",async()=>{
    const sources = Array.from({length:21},(_,index)=>({sourceId:`source-${index}`,name:`Fuente ${index+1}`,language:index<15?"es":"en"}));
    vi.mocked(api.getPublicSources).mockResolvedValue(sources);
    open(); fireEvent.click(screen.getByText("Fuentes habilitadas"));
    expect(await screen.findByText("Fuentes habilitadas · 21")).toBeInTheDocument();
    for (const source of sources) expect(screen.getByText(source.name,{exact:true})).toBeInTheDocument();
    expect(screen.getAllByText("Español")).toHaveLength(15);
    expect(screen.getAllByText("Inglés")).toHaveLength(6);
    expect(api.getPublicSources).toHaveBeenCalledTimes(1);
  });
  it("loads the inventory only when opened and lists registered names",async()=>{
    open();
    expect(api.getPublicSources).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText("Fuentes habilitadas"));
    expect(await screen.findByText("LeerCapitulo")).toBeInTheDocument();
    expect(screen.getByText("MangaOni")).toBeInTheDocument();
    expect(screen.getByText("Fuentes habilitadas · 2")).toBeInTheDocument();
    expect(api.getPublicSources).toHaveBeenCalledTimes(1);
  });
  it("allows a retry without affecting the search screen",async()=>{
    vi.mocked(api.getPublicSources).mockRejectedValueOnce(new Error("offline"));
    open(); fireEvent.click(screen.getByText("Fuentes habilitadas"));
    await screen.findByText("No se pudo consultar la lista. La búsqueda sigue disponible.");
    fireEvent.click(screen.getByRole("button",{name:"Reintentar fuentes"}));
    expect(await screen.findByText("LeerCapitulo")).toBeInTheDocument();
  });
});
