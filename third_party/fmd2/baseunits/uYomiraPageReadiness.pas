unit uYomiraPageReadiness;

{$mode objfpc}{$H+}

interface

uses SysUtils;

type
  TYomiraReadyPage = record
    Ordinal: Integer;
    FileName: string;
  end;
  TYomiraReadyPageArray = array of TYomiraReadyPage;
  TYomiraReadySnapshot = record
    Generation: string;
    Total: Integer;
    Pages: TYomiraReadyPageArray;
  end;

  TYomiraPageReadiness = class
  private
    FLock: TRTLCriticalSection;
    FGeneration: string;
    FFiles: array of string;
  public
    constructor Create;
    destructor Destroy; override;
    procedure Reset;
    procedure SetTotal(const ATotal: Integer);
    procedure MarkReady(const AOrdinal: Integer; const AFileName: string);
    function Snapshot: TYomiraReadySnapshot;
    function Matches(const AGeneration, AFileName: string;
      const ACompleted: Boolean): Boolean;
  end;

implementation

constructor TYomiraPageReadiness.Create;
begin
  inherited Create;
  InitCriticalSection(FLock);
  Reset;
end;

destructor TYomiraPageReadiness.Destroy;
begin
  DoneCriticalSection(FLock);
  inherited Destroy;
end;

procedure TYomiraPageReadiness.Reset;
var
  ID: TGUID;
begin
  if CreateGUID(ID) <> 0 then raise Exception.Create('Could not create page generation.');
  EnterCriticalSection(FLock);
  try
    FGeneration := LowerCase(Copy(GUIDToString(ID), 2, 36));
    SetLength(FFiles, 0);
  finally
    LeaveCriticalSection(FLock);
  end;
end;

procedure TYomiraPageReadiness.SetTotal(const ATotal: Integer);
begin
  if (ATotal < 1) or (ATotal > 10000) then
    raise ERangeError.Create('Invalid page count.');
  EnterCriticalSection(FLock);
  try
    if Length(FFiles) <> 0 then raise Exception.Create('Page plan already set.');
    SetLength(FFiles, ATotal);
  finally
    LeaveCriticalSection(FLock);
  end;
end;

procedure TYomiraPageReadiness.MarkReady(const AOrdinal: Integer;
  const AFileName: string);
begin
  EnterCriticalSection(FLock);
  try
    if (AOrdinal < 1) or (AOrdinal > Length(FFiles)) or (AFileName = '') or
      (ExtractFileName(AFileName) <> AFileName) then
      raise ERangeError.Create('Invalid ready page.');
    if (FFiles[AOrdinal - 1] <> '') and (FFiles[AOrdinal - 1] <> AFileName) then
      raise Exception.Create('Ready page cannot change within a generation.');
    FFiles[AOrdinal - 1] := AFileName;
  finally
    LeaveCriticalSection(FLock);
  end;
end;

function TYomiraPageReadiness.Snapshot: TYomiraReadySnapshot;
var I, Count: Integer;
begin
  EnterCriticalSection(FLock);
  try
    Result.Generation := FGeneration;
    Result.Total := Length(FFiles);
    SetLength(Result.Pages, Result.Total);
    Count := 0;
    for I := 0 to High(FFiles) do
      if FFiles[I] <> '' then
      begin
        Result.Pages[Count].Ordinal := I + 1;
        Result.Pages[Count].FileName := FFiles[I];
        Inc(Count);
      end;
    SetLength(Result.Pages, Count);
  finally
    LeaveCriticalSection(FLock);
  end;
end;

function TYomiraPageReadiness.Matches(const AGeneration, AFileName: string;
  const ACompleted: Boolean): Boolean;
var I: Integer;
begin
  Result := False;
  EnterCriticalSection(FLock);
  try
    if AGeneration <> FGeneration then Exit;
    // A restored completed task uses the existing completed-file validation path.
    if ACompleted then Exit(True);
    for I := 0 to High(FFiles) do
      if FFiles[I] = AFileName then Exit(True);
  finally
    LeaveCriticalSection(FLock);
  end;
end;

end.
