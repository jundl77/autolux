#include "usb_pro.h"
#include <array>
#include <cstring>

DMXUSBPROParamsType PRO_Params{};
FT_HANDLE device_handle=nullptr;

void FTDI_ClosePort() {
    if(device_handle) { FT_Close(device_handle); device_handle=nullptr; }
}
int FTDI_ListDevices() {
    DWORD count=0;
    return FT_ListDevices(&count,nullptr,FT_LIST_NUMBER_ONLY)==FT_OK ? static_cast<int>(count) : 0;
}
int FTDI_SendData(int label,unsigned char* data,int length) {
    if(!device_handle || label<0 || label>255 || length<0 || length>513 || (!data && length)) return NO_RESPONSE;
    std::array<unsigned char,518> packet{};
    packet[0]=DMX_START_CODE; packet[1]=static_cast<unsigned char>(label);
    packet[2]=static_cast<unsigned char>(length); packet[3]=static_cast<unsigned char>(length>>8);
    if(length) std::memcpy(packet.data()+4,data,length);
    packet[4+length]=DMX_END_CODE;
    DWORD written=0;
    const DWORD size=static_cast<DWORD>(length+5);
    const auto status=FT_Write(device_handle,packet.data(),size,&written);
    return status==FT_OK && written==size ? TRUE : NO_RESPONSE;
}
int FTDI_ReceiveData(int label,unsigned char* data,unsigned int expected) {
    if(!device_handle || !data || expected>512) return NO_RESPONSE;
    auto read=[](void* destination,DWORD size) {
        DWORD received=0;
        return FT_Read(device_handle,destination,size,&received)==FT_OK && received==size;
    };
    unsigned char byte=0;
    // Bound malformed/noisy device input as well as every payload copy.
    for(int attempt=0;attempt<64;++attempt) {
        if(!read(&byte,1)) return NO_RESPONSE;
        if(byte==DMX_START_CODE) break;
    }
    if(byte!=DMX_START_CODE) return NO_RESPONSE;
    std::array<unsigned char,3> header{};
    if(!read(header.data(),3) || header[0]!=label) return NO_RESPONSE;
    const unsigned int length=header[1]+256u*header[2];
    if(length<expected || length>512) return NO_RESPONSE;
    std::array<unsigned char,512> payload{};
    if(!read(payload.data(),length) || !read(&byte,1) || byte!=DMX_END_CODE) return NO_RESPONSE;
    std::memcpy(data,payload.data(),expected);
    return TRUE;
}
void FTDI_PurgeBuffer() {
    FT_Purge(device_handle,FT_PURGE_RX|FT_PURGE_TX);
}
uint16_t FTDI_OpenDevice(int index) {
    if(FT_Open(index,&device_handle)!=FT_OK) { device_handle=nullptr; return FALSE; }
    if(FT_SetTimeouts(device_handle,100,100)!=FT_OK) { FTDI_ClosePort(); return FALSE; }
    FT_Purge(device_handle,FT_PURGE_RX);
    unsigned char request[2]{};
    for(int attempt=0;attempt<3;++attempt) {
        if(FTDI_SendData(GET_WIDGET_PARAMS,request,2) &&
           FTDI_ReceiveData(GET_WIDGET_PARAMS_REPLY,reinterpret_cast<unsigned char*>(&PRO_Params),sizeof(PRO_Params)))
            return TRUE;
    }
    FTDI_ClosePort(); return FALSE;
}
