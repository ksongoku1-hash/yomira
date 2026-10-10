unit uYomiraApi;

{$mode objfpc}{$H+}

interface

procedure StartYomiraApi;
procedure StopYomiraApi;

implementation

uses
  Classes, SysUtils, StrUtils, Math, fpjson, jsonparser, fphttpserver, httpdefs, Forms,
  FileUtil, FMDOptions, uBaseUnit, uData, DBDataProcess, WebsiteModules, uDownloadsManager,
  BaseThread, FMDVars, MultiLog, DBUpdater, XQueryEngineHTML, xquery, synacode, SyncObjs;

const
  API_PORT_DEFAULT = 18873;
  API_BODY_LIMIT = 8192;
  API_FILE_LIMIT = 67108864;
  API_PUBLIC_SOURCE_COUNT = 9;
  API_SOURCE_ID = '18f636ec7fdf47fabe95d940ad0b548f';
  API_SOURCE_NAME = 'WebToons';
  API_REF_PREFIX = 'fmd2:v1:';
  API_SOURCE_IDS: array[0..API_PUBLIC_SOURCE_COUNT - 1] of string = (
    '18f636ec7fdf47fabe95d940ad0b548f', '97dcd409f781467eb2da4a8634e5a1d9',
    '9a0a1298ca044dd9886a3446092f285e', 'f31e7a36050c424291c839f0a02d92a4',
    '7103ae6839ea46ec80cdfc2c4b37c803', '9185eb6c49324a849c7d7925a41ef3a3',
    '424747b0135c4240a902eb9cfcb11db9', '03ae42ac9141443fa0711bd401c0203d',
    'K042a85566244b7e836679491ce67ot0');
  API_SOURCE_NAMES: array[0..API_PUBLIC_SOURCE_COUNT - 1] of string = (
    'WebToons', 'CapibaraTraductor', 'Cartel De Manhwas', 'Apoll Comics',
    'Asura Scans', 'TuMangaOnline', 'NovelCoolES', 'InManga', 'ManhwaWeb');
  API_SOURCE_ROOTS: array[0..API_PUBLIC_SOURCE_COUNT - 1] of string = (
    'https://www.webtoons.com', 'https://capibaratraductor.com',
    'https://carteldemanhwas.net', 'https://apollcomics.es',
    'https://asurascans.com', 'https://zonatmo.org', 'https://es.novelcool.com',
    'https://inmanga.com', 'https://www.manhwaweb.com');

type
  ESourceCatalogNotReady = class(Exception);

  TLoopbackHTTPServer = class(TFPHTTPServer)
  public
    property Address;
  end;

  TYomiraApiListener = class(TBaseThread)
  private
    FServer: TLoopbackHTTPServer;
    FActionLock, FCatalogLock: TCriticalSection;
    FToken: string;
    FPort: Word;
    FActionSourceID: string;
    FActionMangaURL: string;
    FActionChapterURL: string;
    FActionChapterTitle: string;
    FActionMangaRef: string;
    FActionChapterRef: string;
    FActionJobID: string;
    FActionFileName: string;
    FActionFilePath: string;
    FActionResult: string;
    FActionError: string;
    FActionKind: Integer;
    procedure OnRequest(Sender: TObject; var ARequest: TFPHTTPConnectionRequest;
      var AResponse: TFPHTTPConnectionResponse);
    procedure OnAcceptIdle(Sender: TObject);
    procedure CreateDownloadOnMainThread;
    procedure GetDownloadStatusOnMainThread;
    procedure CancelDownloadOnMainThread;
    procedure LocateStageFileOnMainThread;
    procedure ShutdownOnMainThread;
    procedure SetJSON(AResponse: TFPHTTPConnectionResponse; ACode: Integer;
      AObject: TJSONObject);
    procedure SetError(AResponse: TFPHTTPConnectionResponse; ACode: Integer;
      const ACodeText: string);
    function SourcesJSON: TJSONObject;
    function SearchJSON(const ABody: string): TJSONObject;
    function MangaJSON(const AReference: string; AChaptersOnly: Boolean): TJSONObject;
    function DownloadJSON(const ABody: string; AResponse: TFPHTTPConnectionResponse): TJSONObject;
    function StatusJSON(const AJobID: string; AResponse: TFPHTTPConnectionResponse): TJSONObject;
    function CancelJSON(const AJobID: string; AResponse: TFPHTTPConnectionResponse): TJSONObject;
    procedure FileResponse(const AJobID, AFileName: string; AResponse: TFPHTTPConnectionResponse);
    function IsAuthorized(const AHeader: string): Boolean;
  protected
    procedure Execute; override;
  public
    constructor Create(const AToken: string; const APort: Word);
  end;

var
  ApiListener: TYomiraApiListener = nil;

function HexByte(const AValue: Byte): string;
begin
  Result := IntToHex(AValue, 2);
end;

function PackReference(const ASourceID, AURL: string): string;
var
  Raw: RawByteString;
  I: Integer;
begin
  Raw := ASourceID + #10 + AURL;
  Result := API_REF_PREFIX;
  for I := 1 to Length(Raw) do
    Result += HexByte(Byte(Raw[I]));
end;

function UnpackReference(const AReference: string; out ASourceID, AURL: string): Boolean;
var
  HexValue: string;
  Raw: RawByteString;
  I, N: Integer;
begin
  Result := False;
  ASourceID := '';
  AURL := '';
  if (Length(AReference) < Length(API_REF_PREFIX) + 6) or
    (Copy(AReference, 1, Length(API_REF_PREFIX)) <> API_REF_PREFIX) then Exit;
  HexValue := Copy(AReference, Length(API_REF_PREFIX) + 1, MaxInt);
  if (Length(HexValue) mod 2 <> 0) or (Length(HexValue) > 8192) then Exit;
  SetLength(Raw, Length(HexValue) div 2);
  for I := 1 to Length(Raw) do
  begin
    if not TryStrToInt('$' + Copy(HexValue, ((I - 1) * 2) + 1, 2), N) then Exit;
    Raw[I] := AnsiChar(N);
  end;
  N := Pos(#10, Raw);
  if (N < 2) or (N >= Length(Raw)) then Exit;
  ASourceID := Copy(Raw, 1, N - 1);
  AURL := Copy(Raw, N + 1, MaxInt);
  Result := (ASourceID <> '') and (AURL <> '');
end;

function PublicSourceIndex(const ASourceID: string): Integer;
var
  I: Integer;
begin
  Result := -1;
  for I := 0 to API_PUBLIC_SOURCE_COUNT - 1 do
    if SameText(ASourceID, API_SOURCE_IDS[I]) then
      Exit(I);
end;

function PublicModule(const ASourceIndex: Integer): TModuleContainer;
begin
  Result := nil;
  if (ASourceIndex < 0) or (ASourceIndex >= API_PUBLIC_SOURCE_COUNT) then Exit;
  Result := Modules.LocateModule(API_SOURCE_IDS[ASourceIndex]);
  if (Result = nil) or (Result.ID <> API_SOURCE_IDS[ASourceIndex]) or
    not SameText(Result.Name, API_SOURCE_NAMES[ASourceIndex]) or
    not SameText(ExcludeTrailingPathDelimiter(Result.RootURL), API_SOURCE_ROOTS[ASourceIndex]) or
    Result.AccountSupport then
    Result := nil;
end;

function IsSafeModuleURL(const AURL, ASourceID: string): Boolean;
var
  LowerURL: string;
begin
  LowerURL := LowerCase(AURL);
  Result := (AURL <> '') and (AURL[1] = '/') and
    (Copy(AURL, 1, 2) <> '//') and (Pos('://', LowerURL) = 0) and
    (Pos('\', AURL) = 0) and (Pos(#0, AURL) = 0) and
    (Pos(#10, AURL) = 0) and (Pos(#13, AURL) = 0) and
    (Pos('..', AURL) = 0) and (PublicSourceIndex(ASourceID) >= 0);
  if Result and SameText(ASourceID, API_SOURCE_ID) then
    Result := StartsStr('/en/', LowerURL) and not StartsStr('/en/canvas/', LowerURL);
end;

function StagingRoot: string;
var
  S: string;
begin
  S := GetEnvironmentVariable('YOMIRA_FMD2_STAGING_ROOT');
  if S = '' then
    S := USERDATA_FOLDER + 'yomira-staging';
  Result := ExcludeTrailingPathDelimiter(ExpandFileName(S));
end;

function JobPath(const AJobID: string): string;
begin
  Result := IncludeTrailingPathDelimiter(StagingRoot) + LowerCase(AJobID);
end;

function IsUnderStagingRoot(const APath: string): Boolean;
var
  Root, Target: string;
begin
  Root := IncludeTrailingPathDelimiter(StagingRoot);
  Target := IncludeTrailingPathDelimiter(ExpandFileName(APath));
  Result := SameText(Copy(Target, 1, Length(Root)), Root);
end;

function IsValidJobID(const AJobID: string): Boolean;
var
  G: TGUID;
begin
  Result := (Length(AJobID) = 36) and (TryStringToGUID('{' + AJobID + '}', G));
end;

function NewJobID: string;
var
  G: TGUID;
begin
  if CreateGUID(G) <> 0 then
    raise Exception.Create('Could not allocate a provider job ID.');
  Result := LowerCase(Copy(GUIDToString(G), 2, 36));
end;

function ParseObject(const AText: string): TJSONObject;
var
  Parser: TJSONParser;
  Data: TJSONData;
begin
  if Length(AText) > API_BODY_LIMIT then
    raise EConvertError.Create('Request body is too large.');
  Parser := TJSONParser.Create(AText);
  try
    Data := Parser.Parse;
    if not (Data is TJSONObject) then
    begin
      Data.Free;
      raise EConvertError.Create('JSON object required.');
    end;
    Result := TJSONObject(Data);
  finally
    Parser.Free;
  end;
end;

function LowerStatus(const AStatus: TDownloadStatusType): string;
begin
  case AStatus of
    STATUS_WAIT: Result := 'queued';
    STATUS_PREPARE, STATUS_DOWNLOAD, STATUS_CONVERT, STATUS_COMPRESS: Result := 'running';
    STATUS_FINISH: Result := 'completed';
    STATUS_STOP: Result := 'cancelled';
    STATUS_PROBLEM, STATUS_FAILED: Result := 'failed';
  else
    Result := 'unknown';
  end;
end;

constructor TYomiraApiListener.Create(const AToken: string; const APort: Word);
begin
  inherited Create(True);
  FreeOnTerminate := False;
  FToken := AToken;
  FPort := APort;
  FActionLock := TCriticalSection.Create;
  FCatalogLock := TCriticalSection.Create;
end;

procedure TYomiraApiListener.OnAcceptIdle(Sender: TObject);
begin
  if Terminated and (FServer <> nil) then
    FServer.Active := False;
end;

procedure TYomiraApiListener.Execute;
begin
  FServer := TLoopbackHTTPServer.Create(nil);
  try
    FServer.Address := '127.0.0.1';
    FServer.Port := FPort;
    FServer.Threaded := True;
    FServer.AcceptIdleTimeout := 250;
    FServer.OnAcceptIdle := @OnAcceptIdle;
    FServer.OnRequest := @OnRequest;
    try
      FServer.Active := True;
    except
      on E: Exception do
        Logger.SendWarning('Yomira FMD2 API could not start: ' + E.Message);
    end;
  finally
    FreeAndNil(FServer);
    FreeAndNil(FActionLock);
    FreeAndNil(FCatalogLock);
  end;
end;

procedure TYomiraApiListener.SetJSON(AResponse: TFPHTTPConnectionResponse;
  ACode: Integer; AObject: TJSONObject);
begin
  AResponse.Code := ACode;
  AResponse.ContentType := 'application/json; charset=utf-8';
  AResponse.Content := AObject.AsJSON;
  AObject.Free;
end;

procedure TYomiraApiListener.SetError(AResponse: TFPHTTPConnectionResponse;
  ACode: Integer; const ACodeText: string);
var
  Root, ErrorObject: TJSONObject;
begin
  Root := TJSONObject.Create;
  ErrorObject := TJSONObject.Create;
  ErrorObject.Add('code', ACodeText);
  Root.Add('error', ErrorObject);
  SetJSON(AResponse, ACode, Root);
end;

function TYomiraApiListener.IsAuthorized(const AHeader: string): Boolean;
var
  Supplied: string;
  I, Diff: Integer;
begin
  Result := False;
  if (Length(AHeader) < 8) or (Copy(AHeader, 1, 7) <> 'Bearer ') then Exit;
  Supplied := Copy(AHeader, 8, MaxInt);
  Diff := Length(Supplied) xor Length(FToken);
  for I := 1 to Min(Length(Supplied), Length(FToken)) do
    Diff := Diff or (Ord(Supplied[I]) xor Ord(FToken[I]));
  Result := Diff = 0;
end;

function TYomiraApiListener.SourcesJSON: TJSONObject;
var
  Root, Item: TJSONObject;
  Data: TJSONArray;
  Capabilities: TJSONArray;
  Module: TModuleContainer;
  I: Integer;
begin
  Root := TJSONObject.Create;
  Data := TJSONArray.Create;
  for I := 0 to API_PUBLIC_SOURCE_COUNT - 1 do
  begin
    Module := PublicModule(I);
    if Module = nil then Continue;
    Item := TJSONObject.Create;
    Item.Add('source_id', Module.ID);
    Item.Add('name', Module.Name);
    Item.Add('base_url', Module.RootURL);
    Item.Add('access', 'public');
    Item.Add('catalog_ready', DBDataFileExist(Module.ID));
    Capabilities := TJSONArray.Create;
    Capabilities.Add('search');
    Capabilities.Add('manga_info');
    Capabilities.Add('chapters');
    Capabilities.Add('download');
    Item.Add('capabilities', Capabilities);
    Data.Add(Item);
  end;
  Root.Add('data', Data);
  Root.Add('api_version', 'v1');
  Root.Add('source_policy', 'public-allowlist-v1');
  Result := Root;
end;

procedure SearchLiveSource(const AQuery: string; const ALimit: Integer;
  const AModule: TModuleContainer; const AData: TJSONArray; const ASeen: TStrings);
var
  Info: TMangaInformation;
  Parser: TXQueryEngineHTML;
  Node: IXQValue;
  URL, Title, SearchURL, Selector, TitleSelector: string;
  SourceIndex: Integer;
  Item: TJSONObject;
begin
  Info := TMangaInformation.Create;
  try
    Info.Module := AModule;
    Info.HTTP.Timeout := 5000;
    Info.HTTP.RetryCount := 0;
    try
      SourceIndex := PublicSourceIndex(AModule.ID);
      case SourceIndex of
        5: begin
          SearchURL := AModule.RootURL + '/biblioteca?title=' +
            EncodeURLElement(AQuery) + '&filter_by=title&page=1';
          Selector := '//div[@id="library-grid"]/div/a';
          TitleSelector := '(.//h4)[1]';
        end;
        6: begin
          SearchURL := AModule.RootURL + '/search/?name=' + EncodeURLElement(AQuery);
          Selector := '//div[@class="book-info"]/a';
          TitleSelector := '@title';
        end;
        7: begin
          Selector := '//a[contains(@class,"manga-result")]';
          TitleSelector := '(.//h4)[1]';
          if not Info.HTTP.POST(AModule.RootURL + '/manga/getMangasConsultResult',
            'filter%5Bgeneres%5D%5B%5D=-1&filter%5BqueryString%5D=' +
            EncodeURLElement(AQuery) + '&filter%5Bskip%5D=0&filter%5Btake%5D=' +
            IntToStr(ALimit) + '&filter%5Bsortby%5D=5&filter%5BbroadcastStatus%5D=0') then Exit;
        end;
        8: begin
          SearchURL := 'https://manhwawebbackend-production.up.railway.app/manhwa/library?buscar=' +
            EncodeURLElement(AQuery) + '&order_item=alfabetico&order_dir=asc&page=0';
          Selector := 'json(*).data()';
          TitleSelector := 'the_real_name';
        end;
        else Exit;
      end;
      // ponytail: first live result page; paginate when more than 50 matches are needed.
      if (SourceIndex <> 7) and not Info.HTTP.GET(SearchURL) then Exit;
      if Info.HTTP.ResultCode <> 200 then Exit;
      Parser := TXQueryEngineHTML.Create(Info.HTTP.Document);
      try
        for Node in Parser.XPath(Selector) do
        begin
          if AData.Count >= ALimit then Break;
          if SourceIndex = 8 then
          begin
            if Parser.XPathString('_tipo', Node) = 'novela' then Continue;
            URL := '/manhwa/' + Parser.XPathString('_id', Node);
          end
          else URL := Node.toNode.getAttribute('href');
          if StartsStr(AModule.RootURL + '/', URL) then
            URL := RemoveHostFromURL(URL);
          Title := Trim(Parser.XPathString(TitleSelector, Node));
          if (SourceIndex = 6) and (Pos('novela', LowerCase(Title)) > 0) then Continue;
          if not IsSafeModuleURL(URL, AModule.ID) or
            ((SourceIndex = 5) and not StartsStr('/library/', URL)) or (Title = '') or
            (ASeen.IndexOf(URL) >= 0) then Continue;
          Item := TJSONObject.Create;
          Item.Add('manga_ref', PackReference(AModule.ID, URL));
          Item.Add('source_id', AModule.ID);
          Item.Add('source_name', AModule.Name);
          Item.Add('title', Title);
          Item.Add('remote_url', FillHost(AModule.RootURL, URL));
          AData.Add(Item);
          ASeen.Add(URL);
        end;
      finally
        Parser.Free;
      end;
    except
      // Live search is optional: preserve the local catalog on source failure.
      on E: Exception do SendLog('Yomira ' + AModule.Name + ' live search unavailable: ' + E.ClassName);
    end;
  finally
    Info.Free;
  end;
end;

function TYomiraApiListener.SearchJSON(const ABody: string): TJSONObject;
var
  Input, Root, Item: TJSONObject;
  Data: TJSONArray;
  SourceData: array[0..API_PUBLIC_SOURCE_COUNT - 1] of TJSONArray;
  Query: string;
  Limit, I, Count, SourceIndex, IndexedCount: Integer;
  Processor: TDBDataProcess;
  Module: TModuleContainer;
  URL, SourceFilter: string;
  Seen: TStringList;
begin
  for SourceIndex := 0 to API_PUBLIC_SOURCE_COUNT - 1 do
    SourceData[SourceIndex] := nil;
  Input := ParseObject(ABody);
  Seen := TStringList.Create;
  try
    SourceFilter := Input.Get('source_id', '*');
    if (SourceFilter <> '*') and (PublicSourceIndex(SourceFilter) < 0) then
      raise EConvertError.Create('Source is not in the public-source allowlist.');
    Query := Trim(Input.Get('query', ''));
    if (Query = '') or (Length(Query) > 200) then
      raise EConvertError.Create('Query must contain 1 to 200 characters.');
    Limit := Input.Get('limit', 25);
    if (Limit < 1) or (Limit > 50) then
      raise EConvertError.Create('Limit must be between 1 and 50.');
    Root := TJSONObject.Create;
    Data := TJSONArray.Create;
    IndexedCount := 0;
    for SourceIndex := 0 to API_PUBLIC_SOURCE_COUNT - 1 do
    begin
      SourceData[SourceIndex] := TJSONArray.Create;
      if (SourceFilter <> '*') and not SameText(SourceFilter, API_SOURCE_IDS[SourceIndex]) then
        Continue;
      Module := PublicModule(SourceIndex);
      if Module = nil then Continue;
      Seen.Clear;
      if SourceIndex >= 5 then
        SearchLiveSource(Query, Limit, Module, SourceData[SourceIndex], Seen);
      Inc(IndexedCount, SourceData[SourceIndex].Count);
      if not DBDataFileExist(API_SOURCE_IDS[SourceIndex]) then Continue;
      Processor := TDBDataProcess.Create;
      try
        if not Processor.Open(API_SOURCE_IDS[SourceIndex]) then Continue;
        try
          Processor.Search(Query);
          Count := Processor.RecordCount;
          Inc(IndexedCount, Count);
          for I := 0 to Count - 1 do
          begin
            if SourceData[SourceIndex].Count >= Limit then Break;
            URL := Processor.Value[I, DATA_PARAM_LINK];
            if not IsSafeModuleURL(URL, API_SOURCE_IDS[SourceIndex]) then Continue;
            if Seen.IndexOf(URL) >= 0 then Continue;
            Seen.Add(URL);
            Item := TJSONObject.Create;
            Item.Add('manga_ref', PackReference(API_SOURCE_IDS[SourceIndex], URL));
            Item.Add('source_id', API_SOURCE_IDS[SourceIndex]);
            Item.Add('source_name', API_SOURCE_NAMES[SourceIndex]);
            Item.Add('title', Processor.Value[I, DATA_PARAM_TITLE]);
            Item.Add('remote_url', FillHost(Module.RootURL, URL));
            SourceData[SourceIndex].Add(Item);
          end;
        finally
          Processor.Close;
        end;
      finally
        Processor.Free;
      end;
    end;
    repeat
      Count := Data.Count;
      for SourceIndex := 0 to API_PUBLIC_SOURCE_COUNT - 1 do
      begin
        if Data.Count >= Limit then Break;
        if SourceData[SourceIndex].Count > 0 then
          Data.Add(SourceData[SourceIndex].Extract(0));
      end;
    until (Data.Count = Count) or (Data.Count >= Limit);
    Root.Add('data', Data);
    Root.Add('query', Query);
    Root.Add('indexed_count', IndexedCount);
    Root.Add('source_id', SourceFilter);
    Exit(Root);
  finally
    for SourceIndex := 0 to API_PUBLIC_SOURCE_COUNT - 1 do
      SourceData[SourceIndex].Free;
    Input.Free;
    Seen.Free;
  end;
end;

function TYomiraApiListener.MangaJSON(const AReference: string;
  AChaptersOnly: Boolean): TJSONObject;
var
  SourceID, URL: string;
  Module: TModuleContainer;
  Info: TMangaInformation;
  Root, Manga, Chapter: TJSONObject;
  Data: TJSONArray;
  I, SourceIndex: Integer;
begin
  if not UnpackReference(AReference, SourceID, URL) or
    (PublicSourceIndex(SourceID) < 0) or not IsSafeModuleURL(URL, SourceID) then
    raise EConvertError.Create('Invalid or disallowed manga reference.');
  SourceIndex := PublicSourceIndex(SourceID);
  Module := PublicModule(SourceIndex);
  if Module = nil then
    raise EConvertError.Create('Allowlisted module is not loaded.');
  Info := TMangaInformation.Create;
  try
    Info.Module := Module;
    if Info.GetInfoFromURL(URL) <> NO_ERROR then
      raise Exception.Create('Public manga metadata could not be retrieved.');
    Root := TJSONObject.Create;
    if not AChaptersOnly then
    begin
      if not IsSafeModuleURL(Info.MangaInfo.Link, SourceID) then
        raise EConvertError.Create('Source returned an invalid manga link.');
      Manga := TJSONObject.Create;
      Manga.Add('manga_ref', PackReference(SourceID, Info.MangaInfo.Link));
      Manga.Add('source_id', SourceID);
      Manga.Add('title', Info.MangaInfo.Title);
      Manga.Add('cover_url', Info.MangaInfo.CoverLink);
      Manga.Add('summary', Info.MangaInfo.Summary);
      Manga.Add('authors', Info.MangaInfo.Authors);
      Manga.Add('artists', Info.MangaInfo.Artists);
      Manga.Add('genres', Info.MangaInfo.Genres);
      Manga.Add('status', Info.MangaInfo.Status);
      Root.Add('manga', Manga);
    end;
    Data := TJSONArray.Create;
    for I := 0 to Info.MangaInfo.ChapterLinks.Count - 1 do
    begin
      URL := Info.MangaInfo.ChapterLinks[I];
      if not IsSafeModuleURL(URL, SourceID) then Continue;
      Chapter := TJSONObject.Create;
      Chapter.Add('chapter_ref', PackReference(SourceID, URL));
      Chapter.Add('title', Info.MangaInfo.ChapterNames[I]);
      Chapter.Add('ordinal', I + 1);
      Data.Add(Chapter);
    end;
    Root.Add('chapters', Data);
    Result := Root;
  finally
    Info.Free;
  end;
end;

function TYomiraApiListener.DownloadJSON(const ABody: string;
  AResponse: TFPHTTPConnectionResponse): TJSONObject;
var
  Input: TJSONObject;
  ModuleID, MangaSourceID, MangaURL, ChapterURL, RequestID: string;
  Root: TJSONObject;
begin
  Input := ParseObject(ABody);
  try
    RequestID := Input.Get('request_id', '');
    if (RequestID <> '') and not IsValidJobID(RequestID) then
      raise EConvertError.Create('Request ID must be a UUID.');
    FActionJobID := LowerCase(RequestID);
    FActionMangaRef := Input.Get('manga_ref', '');
    FActionChapterRef := Input.Get('chapter_ref', '');
    FActionChapterTitle := Input.Get('chapter_title', 'Chapter');
    if (Length(FActionChapterTitle) > 256) or (FActionChapterTitle = '') then
      raise EConvertError.Create('Chapter title must contain 1 to 256 characters.');
    if not UnpackReference(FActionMangaRef, MangaSourceID, MangaURL) or
      (PublicSourceIndex(MangaSourceID) < 0) or
      not IsSafeModuleURL(MangaURL, MangaSourceID) or
      not UnpackReference(FActionChapterRef, ModuleID, ChapterURL) or
      (ModuleID <> MangaSourceID) or
      not IsSafeModuleURL(ChapterURL, MangaSourceID) then
      raise EConvertError.Create('Invalid or disallowed download reference.');
    FActionSourceID := MangaSourceID;
    FActionMangaURL := MangaURL;
    FActionChapterURL := ChapterURL;
    FActionError := '';
    FActionResult := '';
    Synchronize(@CreateDownloadOnMainThread);
    if FActionError <> '' then
      raise EConvertError.Create(FActionError);
    Root := TJSONObject.Create;
    Root.Add('job_id', FActionJobID);
    Root.Add('status', 'queued');
    Root.Add('staging_path', IncludeTrailingPathDelimiter(ExpandFileName(JobPath(FActionJobID))));
    Result := Root;
  finally
    Input.Free;
  end;
end;

procedure TYomiraApiListener.CreateDownloadOnMainThread;
var
  Module: TModuleContainer;
  Task: TTaskContainer;
  Target, SavedTo: string;
  I, ExistingIndex, SourceIndex: Integer;
  Existing, RetryExisting: Boolean;
begin
  FActionError := '';
  FActionResult := '';
  Task := nil;
  Target := '';
  ExistingIndex := -1;
  Existing := False;
  RetryExisting := False;
  try
    SourceIndex := PublicSourceIndex(FActionSourceID);
    Module := PublicModule(SourceIndex);
    if Module = nil then
      raise Exception.Create('Allowlisted module is not loaded.');
    if not IsSafeModuleURL(FActionMangaURL, FActionSourceID) or
      not IsSafeModuleURL(FActionChapterURL, FActionSourceID) then
      raise Exception.Create('Reference failed the source URL policy.');
    ForceDirectories(StagingRoot);
    if not DirectoryExists(StagingRoot) then
      raise Exception.Create('Staging root is not available.');
    if FActionJobID = '' then
      FActionJobID := NewJobID;
    Target := JobPath(FActionJobID);
    SavedTo := IncludeTrailingPathDelimiter(ExpandFileName(Target));
    DLManager.Lock;
    try
      for I := 0 to DLManager.Items.Count - 1 do
      begin
        if SameText(ExpandFileName(DLManager.Items[I].DownloadInfo.SaveTo), SavedTo) then
        begin
          ExistingIndex := I;
          Existing := True;
          Task := DLManager.Items[I];
          if (Task.DownloadInfo.Module = nil) or
            (TModuleContainer(Task.DownloadInfo.Module).ID <> FActionSourceID) or
            (Task.DownloadInfo.Link <> FActionMangaURL) or
            (Task.ChapterLinks.Count <> 1) or
            (Task.ChapterLinks[0] <> FActionChapterURL) then
            raise Exception.Create('Request ID is already assigned to another download.');
          RetryExisting := DLManager.Items[I].Status in [STATUS_STOP, STATUS_PROBLEM, STATUS_FAILED];
          Break;
        end;
      end;
      if not Existing then
      begin
        if DirectoryExists(Target) or FileExists(Target) or not ForceDirectories(Target) then
          raise Exception.Create('Could not create an isolated staging folder.');
        Task := DLManager.AddTask;
        Task.RawOutput := True;
        Task.DownloadInfo.Module := Module;
        Task.DownloadInfo.Link := FActionMangaURL;
        Task.DownloadInfo.Title := 'Yomira FMD2 staging download';
        Task.DownloadInfo.SaveTo := SavedTo;
        Task.DownloadInfo.DateAdded := Now;
        Task.DownloadInfo.DateLastDownloaded := Now;
        Task.DownloadInfo.Progress := '0/0';
        Task.DownloadInfo.Status := 'Waiting';
        Task.ChapterLinks.Add(FActionChapterURL);
        Task.ChapterNames.Add(FActionChapterTitle);
        Task.CustomFileName := '%FILENAME%';
        Task.Status := STATUS_WAIT;
        Task.DBInsert;
      end;
    finally
      DLManager.UnLock;
    end;
    if Existing then
    begin
      if RetryExisting then
        DLManager.RedownloadTask(ExistingIndex);
      DLManager.CheckAndActiveTask;
      FActionResult := FActionJobID;
      Exit;
    end;
    DLManager.CheckAndActiveTask;
    FActionResult := FActionJobID;
  except
    on E: Exception do
    begin
      FActionError := 'FMD2 could not queue the staging download.';
      if (Task <> nil) and not Existing then
      begin
        DLManager.Lock;
        try
          DLManager.Items.Remove(Task);
          Task.Free;
        finally
          DLManager.UnLock;
        end;
      end;
      if not Existing and (Target <> '') and DirectoryExists(Target) then
        RemoveDir(Target);
      Logger.SendWarning('Yomira FMD2 API could not queue task: ' + E.Message);
    end;
  end;
end;

procedure TYomiraApiListener.LocateStageFileOnMainThread;
var
  I: Integer;
  Task: TTaskContainer;
  Search: TSearchRec;
  Folder, Candidate: string;
begin
  FActionError := '';
  FActionFilePath := '';
  Folder := IncludeTrailingPathDelimiter(ExpandFileName(JobPath(FActionJobID)));
  DLManager.Lock;
  try
    for I := 0 to DLManager.Items.Count - 1 do
    begin
      Task := DLManager.Items[I];
      if SameText(ExpandFileName(Task.DownloadInfo.SaveTo), Folder) and
        Task.RawOutput and (Task.Status = STATUS_FINISH) then
      begin
        Candidate := ExpandFileName(Folder + FActionFileName);
        if not IsUnderStagingRoot(Candidate) then
          Break;
        if (FindFirst(Candidate, faAnyFile, Search) = 0) then
        try
          if ((Search.Attr and faDirectory) = 0) and
            (Search.Size > 0) and (Search.Size <= API_FILE_LIMIT) then
            FActionFilePath := Candidate;
        finally
          FindClose(Search);
        end;
        Break;
      end;
    end;
  finally
    DLManager.UnLock;
  end;
  if FActionFilePath = '' then
    FActionError := 'Completed staging image was not found.';
end;

procedure TYomiraApiListener.ShutdownOnMainThread;
begin
  Application.Terminate;
end;

procedure TYomiraApiListener.FileResponse(const AJobID, AFileName: string;
  AResponse: TFPHTTPConnectionResponse);
var
  I: Integer;
  Ext: string;
  Stream: TFileStream;
begin
  if not IsValidJobID(AJobID) or (AFileName = '') or
    (Length(AFileName) > 128) then
  begin
    SetError(AResponse, 404, 'not_found');
    Exit;
  end;
  if (AFileName[1] = '.') or
    (Pos('..', AFileName) > 0) or (Pos('/', AFileName) > 0) or
    (Pos('\', AFileName) > 0) then
  begin
    SetError(AResponse, 404, 'not_found');
    Exit;
  end;
  for I := 1 to Length(AFileName) do
    if not (AFileName[I] in ['a'..'z', 'A'..'Z', '0'..'9', '.', '_', '-']) then
    begin
      SetError(AResponse, 404, 'not_found');
      Exit;
    end;
  Ext := LowerCase(ExtractFileExt(AFileName));
  if not ((Ext = '.jpg') or (Ext = '.jpeg') or (Ext = '.png') or (Ext = '.webp')) then
  begin
    SetError(AResponse, 404, 'not_found');
    Exit;
  end;
  FActionJobID := LowerCase(AJobID);
  FActionFileName := AFileName;
  Synchronize(@LocateStageFileOnMainThread);
  if FActionError <> '' then
  begin
    SetError(AResponse, 404, 'not_found');
    Exit;
  end;
  Stream := nil;
  try
    Stream := TFileStream.Create(FActionFilePath, fmOpenRead or fmShareDenyNone);
    if (Stream.Size <= 0) or (Stream.Size > API_FILE_LIMIT) then
    begin
      SetError(AResponse, 404, 'not_found');
      Exit;
    end;
    AResponse.Code := 200;
    AResponse.CacheControl := 'no-store';
    AResponse.ContentType := 'image/jpeg';
    if Ext = '.png' then AResponse.ContentType := 'image/png'
    else if Ext = '.webp' then AResponse.ContentType := 'image/webp';
    AResponse.ContentLength := Stream.Size;
    AResponse.ContentStream := Stream;
    AResponse.FreeContentStream := True;
    Stream := nil;
  finally
    Stream.Free;
  end;
end;

function TYomiraApiListener.StatusJSON(const AJobID: string;
  AResponse: TFPHTTPConnectionResponse): TJSONObject;
begin
  if not IsValidJobID(AJobID) then
    raise EConvertError.Create('Invalid provider job ID.');
  FActionJobID := LowerCase(AJobID);
  FActionError := '';
  FActionResult := '';
  Synchronize(@GetDownloadStatusOnMainThread);
  if FActionError <> '' then
    raise EConvertError.Create(FActionError);
  Result := TJSONObject(GetJSON(FActionResult));
end;

procedure TYomiraApiListener.GetDownloadStatusOnMainThread;
var
  I, Found: Integer;
  Task: TTaskContainer;
  Root: TJSONObject;
  Names: TJSONArray;
  Search: TSearchRec;
  Path: string;
begin
  FActionError := '';
  FActionResult := '';
  Path := IncludeTrailingPathDelimiter(ExpandFileName(JobPath(FActionJobID)));
  Found := -1;
  DLManager.Lock;
  try
    for I := 0 to DLManager.Items.Count - 1 do
      if SameText(ExpandFileName(DLManager.Items[I].DownloadInfo.SaveTo), Path) then
      begin
        Found := I;
        Break;
      end;
    if Found < 0 then
      raise Exception.Create('Provider job was not found.');
    Task := DLManager.Items[Found];
    Root := TJSONObject.Create;
    Root.Add('job_id', FActionJobID);
    Root.Add('status', LowerStatus(Task.Status));
    Root.Add('progress', Task.DownloadInfo.Progress);
    if Task.Status = STATUS_FINISH then
      Root.Add('completed_pages', Task.PageNumber)
    else
      Root.Add('completed_pages', Task.DownCounter);
    Root.Add('total_pages', Task.PageNumber);
    Root.Add('staging_path', Path);
    Names := TJSONArray.Create;
    if FindFirst(Path + '*', faAnyFile, Search) = 0 then
    try
      repeat
        if ((Search.Attr and faDirectory) = 0) and
          (Search.Name <> '.') and (Search.Name <> '..') then
          Names.Add(Search.Name);
      until FindNext(Search) <> 0;
    finally
      FindClose(Search);
    end;
    Root.Add('files', Names);
    FActionResult := Root.AsJSON;
    Root.Free;
  finally
    DLManager.UnLock;
  end;
end;

function TYomiraApiListener.CancelJSON(const AJobID: string;
  AResponse: TFPHTTPConnectionResponse): TJSONObject;
begin
  if not IsValidJobID(AJobID) then
    raise EConvertError.Create('Invalid provider job ID.');
  FActionJobID := LowerCase(AJobID);
  FActionError := '';
  FActionResult := '';
  Synchronize(@CancelDownloadOnMainThread);
  if FActionError <> '' then
    raise EConvertError.Create(FActionError);
  Result := TJSONObject(GetJSON(FActionResult));
end;

procedure TYomiraApiListener.CancelDownloadOnMainThread;
var
  I, Found: Integer;
  Root: TJSONObject;
  Path: string;
begin
  FActionError := '';
  FActionResult := '';
  Path := IncludeTrailingPathDelimiter(ExpandFileName(JobPath(FActionJobID)));
  Found := -1;
  DLManager.Lock;
  try
    for I := 0 to DLManager.Items.Count - 1 do
      if SameText(ExpandFileName(DLManager.Items[I].DownloadInfo.SaveTo), Path) then
      begin
        Found := I;
        Break;
      end;
    if Found < 0 then
      raise Exception.Create('Provider job was not found.');
    DLManager.StopTask(Found, False);
  finally
    DLManager.UnLock;
  end;
  DLManager.CheckAndActiveTask;
  Root := TJSONObject.Create;
  Root.Add('job_id', FActionJobID);
  Root.Add('status', 'cancellation_requested');
  FActionResult := Root.AsJSON;
  Root.Free;
end;

procedure TYomiraApiListener.OnRequest(Sender: TObject;
  var ARequest: TFPHTTPConnectionRequest;
  var AResponse: TFPHTTPConnectionResponse);
var
  Path, Method, Ref, JobID, FileName: string;
  PosEnd, FileMarker: SizeInt;
  JSON: TJSONObject;
  ControlRequest: Boolean;
begin
  AResponse.ContentType := 'application/json; charset=utf-8';
  Path := ARequest.PathInfo;
  if Path = '' then Path := ARequest.URI;
  PosEnd := Pos('?', Path);
  if PosEnd > 0 then Delete(Path, PosEnd, MaxInt);
  Method := UpperCase(ARequest.Method);
  ControlRequest := StartsStr('/v1/downloads', Path);
  if ControlRequest then FActionLock.Acquire;
  try
  if StartsStr('/v1/downloads/', Path) then
  begin
    Ref := Copy(Path, Length('/v1/downloads/') + 1, MaxInt);
    FileMarker := Pos('/files/', Ref);
    if FileMarker > 0 then
    begin
      JobID := Copy(Ref, 1, FileMarker - 1);
      FileName := Copy(Ref, FileMarker + Length('/files/'), MaxInt);
      if Method <> 'GET' then
        SetError(AResponse, 405, 'method_not_allowed')
      else
        FileResponse(JobID, FileName, AResponse);
      Exit;
    end;
  end;
  if not IsAuthorized(ARequest.Authorization) then
  begin
    SetError(AResponse, 401, 'unauthorized');
    Exit;
  end;
  if ARequest.ContentLength > API_BODY_LIMIT then
  begin
    SetError(AResponse, 413, 'request_too_large');
    Exit;
  end;
  try
    if (Method = 'GET') and (Path = '/v1/sources') then
      JSON := SourcesJSON
    else if (Method = 'POST') and (Path = '/v1/search') then
    begin
      if not FCatalogLock.TryEnter then
      begin
        SetError(AResponse, 503, 'catalog_busy');
        Exit;
      end;
      try
        JSON := SearchJSON(ARequest.Content);
      finally
        FCatalogLock.Release;
      end;
    end
    else if (Method = 'POST') and (Path = '/v1/downloads') then
    begin
      JSON := DownloadJSON(ARequest.Content, AResponse);
      SetJSON(AResponse, 202, JSON);
      Exit;
    end
    else if (Method = 'POST') and (Path = '/v1/shutdown') then
    begin
      JSON := TJSONObject.Create;
      JSON.Add('status', 'shutting_down');
      SetJSON(AResponse, 202, JSON);
      TThread.Queue(Self, @ShutdownOnMainThread);
      Exit;
    end
    else if (Method = 'GET') and StartsStr('/v1/manga/', Path) then
    begin
      if not FCatalogLock.TryEnter then
      begin
        SetError(AResponse, 503, 'catalog_busy');
        Exit;
      end;
      try
      Ref := Copy(Path, Length('/v1/manga/') + 1, MaxInt);
      if AnsiEndsStr('/chapters', Ref) then
      begin
        Delete(Ref, Length(Ref) - Length('/chapters') + 1, MaxInt);
        JSON := MangaJSON(Ref, True);
      end
      else
        JSON := MangaJSON(Ref, False);
      finally
        FCatalogLock.Release;
      end;
    end
    else if StartsStr('/v1/downloads/', Path) then
    begin
      JobID := Copy(Path, Length('/v1/downloads/') + 1, MaxInt);
      if Method = 'GET' then JSON := StatusJSON(JobID, AResponse)
      else if Method = 'DELETE' then JSON := CancelJSON(JobID, AResponse)
      else
      begin
        SetError(AResponse, 405, 'method_not_allowed');
        Exit;
      end;
    end
    else
    begin
      SetError(AResponse, 404, 'not_found');
      Exit;
    end;
    SetJSON(AResponse, 200, JSON);
  except
    on E: ESourceCatalogNotReady do
      SetError(AResponse, 503, 'catalog_not_ready');
    on E: EConvertError do
      SetError(AResponse, 400, 'invalid_request');
    on E: Exception do
    begin
      Logger.SendWarning('Yomira FMD2 API request failed: ' + E.Message);
      SetError(AResponse, 502, 'provider_operation_failed');
    end;
  end;
  finally
    if ControlRequest then FActionLock.Release;
  end;
end;

function ConstantTimeToken(const AToken: string): Boolean;
begin
  Result := Length(AToken) >= 32;
end;

procedure StartYomiraApi;
var
  Token: string;
  PortValue: Integer;
  Module: TModuleContainer;
  I: Integer;
begin
  if ApiListener <> nil then Exit;
  Token := GetEnvironmentVariable('YOMIRA_FMD2_API_TOKEN');
  if not ConstantTimeToken(Token) then
  begin
    Logger.Send('Yomira FMD2 API disabled: set a random 32-character YOMIRA_FMD2_API_TOKEN.');
    Exit;
  end;
  PortValue := StrToIntDef(GetEnvironmentVariable('YOMIRA_FMD2_API_PORT'), API_PORT_DEFAULT);
  if (PortValue < 1024) or (PortValue > 65535) then
    PortValue := API_PORT_DEFAULT;
  for I := 0 to API_PUBLIC_SOURCE_COUNT - 1 do
  begin
    Module := Modules.LocateModule(API_SOURCE_IDS[I]);
    if Module <> nil then Module.AllowBypass := False;
    Module := PublicModule(I);
    if (Module <> nil) and not DBDataFileExist(API_SOURCE_IDS[I]) then
    begin
      if DBUpdaterThread = nil then
      begin
        DBUpdaterThread := TDBUpdaterThread.Create;
        DBUpdaterThread.Silent := True;
      end;
      DBUpdaterThread.Add(Module);
    end;
  end;
  if DBUpdaterThread <> nil then DBUpdaterThread.Start;
  ApiListener := TYomiraApiListener.Create(Token, Word(PortValue));
  ApiListener.Start;
end;

procedure StopYomiraApi;
begin
  if ApiListener = nil then Exit;
  ApiListener.Terminate;
  ApiListener.WaitFor;
  FreeAndNil(ApiListener);
end;

end.
