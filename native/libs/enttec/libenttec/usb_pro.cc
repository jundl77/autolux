#include "usb_pro.h"

// old school globals
DMXUSBPROParamsType PRO_Params;
FT_HANDLE device_handle = NULL ;

/* Function : FTDI_ClosePort
 * Author	: ENTTEC
 * Purpose  : Closes the Open DMX USB PRO Device Handle
 * Parameters: none
 **/
void FTDI_ClosePort()
{
	if (device_handle != NULL)
		FT_Close(device_handle);
}

/* Function : FTDI_ListDevices
 * Author	: ENTTEC
 * Purpose  : Returns the no. of PRO's conneced to the PC
 * Parameters: none
 **/
int FTDI_ListDevices()
{
	long version;
	FT_STATUS ftStatus;
	DWORD numDevs=0;
/*
	uint8_t major_ver,minor_ver,build_ver;
	// D2XX Library Version
	ftStatus = FT_GetLibraryVersion((LPDWORD)&version);
	Sleep(1750);
	if (ftStatus == FT_OK) 
	{
		major_ver = (uint8_t) version >> 16;
		minor_ver = (uint8_t) version >> 8;
		build_ver = (uint8_t) version & 0xFF;
		// Test what version Library we are using
		printf("\n D2XX Library Version:: %02X.%02X.%02X ",major_ver,minor_ver,build_ver);
	}
	else
	{
		printf("Unable to Get D2XX Library Version") ;
		return NO_RESPONSE;
	}
*/
	ftStatus = FT_ListDevices((PVOID)&numDevs,NULL,FT_LIST_NUMBER_ONLY);
	if(ftStatus == FT_OK)
		return numDevs;
	return NO_RESPONSE;
}


/* Function : FTDI_SendData
 * Author	: ENTTEC
 * Purpose  : Send Data (DMX or other packets) to the PRO
 * Parameters: Label, Pointer to Data Structure, Length of Data
 **/
int FTDI_SendData(int label, unsigned char *data, int length)
{
	unsigned char end_code = DMX_END_CODE;
	FT_STATUS res = 0;
	DWORD bytes_to_write = length;
	DWORD bytes_written = 0;
	HANDLE event = NULL;
	int size=0;
	// Form Packet Header
	unsigned char header[DMX_HEADER_LENGTH];
	header[0] = DMX_START_CODE;
	header[1] = label;
	header[2] = length & OFFSET;
	header[3] = length >> BYTE_LENGTH;
	// Write The Header
	res = FT_Write(	device_handle,(unsigned char *)header,DMX_HEADER_LENGTH,&bytes_written);
	if (bytes_written != DMX_HEADER_LENGTH) return  NO_RESPONSE;
	// Write The Data
	res = FT_Write(	device_handle,(unsigned char *)data,length,&bytes_written);
	if (bytes_written != length) return  NO_RESPONSE;
	// Write End Code
	res = FT_Write(	device_handle,(unsigned char *)&end_code,ONE_BYTE,&bytes_written);
	if (bytes_written != ONE_BYTE) return  NO_RESPONSE;
	if (res == FT_OK)
		return TRUE;
	else
		return FALSE;
}

/* Function : FTDI_ReceiveData
 * Author	: ENTTEC
 * Purpose  : Receive Data (DMX or other packets) from the PRO
 * Parameters: Label, Pointer to Data Structure, Length of Data
 **/
int FTDI_ReceiveData(int label, unsigned char *data, unsigned int expected_length)
{

	FT_STATUS res = 0;
	DWORD length = 0;
	DWORD bytes_to_read = 1;
	DWORD bytes_read =0;
	unsigned char byte = 0;
	HANDLE event = NULL;
	char buffer[600];
	// Check for Start Code and matching Label
	while (byte != label)
	{
		while (byte != DMX_START_CODE)
		{
			res = FT_Read(device_handle,(unsigned char *)&byte,ONE_BYTE,&bytes_read);
			if(bytes_read== NO_RESPONSE) return  NO_RESPONSE;
		}
		res = FT_Read(device_handle,(unsigned char *)&byte,ONE_BYTE,&bytes_read);
		if (bytes_read== NO_RESPONSE) return  NO_RESPONSE;
	}
	// Read the rest of the Header Byte by Byte -- Get Length
	res = FT_Read(device_handle,(unsigned char *)&byte,ONE_BYTE,&bytes_read);
	if (bytes_read== NO_RESPONSE) return  NO_RESPONSE;
	length = byte;
	res = FT_Read(device_handle,(unsigned char *)&byte,ONE_BYTE,&bytes_read);
	if (res != FT_OK) return  NO_RESPONSE;
	length += ((uint32_t)byte)<<BYTE_LENGTH;
	// Check Length is not greater than allowed
	if (length > DMX_PACKET_SIZE)
		return  NO_RESPONSE;
	// Read the actual Response Data
	res = FT_Read(device_handle,buffer,length,&bytes_read);
	if(bytes_read != length) return  NO_RESPONSE;
	// Check The End Code
	res = FT_Read(device_handle,(unsigned char *)&byte,ONE_BYTE,&bytes_read);
	if(bytes_read== NO_RESPONSE) return  NO_RESPONSE;
	if (byte != DMX_END_CODE) return  NO_RESPONSE;
	// Copy The Data read to the buffer passed
	memcpy(data,buffer,expected_length);
	return TRUE;
}

// Read a DMX packet
uint8_t FTDI_RxDMX(uint8_t label, unsigned char *data, uint32_t expected_length)
{

	FT_STATUS res = 0;
	DWORD length = 0;
	DWORD bytes_to_read = 1;
	DWORD bytes_read =0;
	unsigned char byte = 0;
	unsigned char header[3];
	HANDLE event = NULL;
	char buffer[600];
	// Check for Start Code and matching Label
	while (byte != DMX_START_CODE)
	{
		res = FT_Read(device_handle,(unsigned char *)&byte,ONE_BYTE,&bytes_read);
		if(bytes_read== NO_RESPONSE) return  NO_RESPONSE;
	}
	res = FT_Read(device_handle,header,3,&bytes_read);
	if(bytes_read== NO_RESPONSE) return  NO_RESPONSE;
	if(header[0] != label) return NO_RESPONSE;
	length = header[1];
	length += ((uint32_t)header[2])<<BYTE_LENGTH;
	length += 1;
	// Check Length is not greater than allowed
	if (length > DMX_PACKET_SIZE +2)
		return  NO_RESPONSE;
	// Read the actual Response Data
	res = FT_Read(device_handle,buffer,length,&bytes_read);
	if(bytes_read != length) return  NO_RESPONSE;
	// Check The End Code
	if (buffer[length-1] != DMX_END_CODE) return  NO_RESPONSE;
	// Copy The Data read to the buffer passed
	memcpy(data,buffer,expected_length);
	return TRUE;
}
/* Function : FTDI_PurgeBuffer
 * Author	: ENTTEC
 * Purpose  : Clears the buffer used internally by the PRO
 * Parameters: none
 **/
void FTDI_PurgeBuffer()
{
	FT_Purge (device_handle,FT_PURGE_TX);
	FT_Purge (device_handle,FT_PURGE_RX);
}


/* Function : FTDI_OpenDevice
 * Author	: ENTTEC
 * Purpose  : Opens the PRO; Tests various parameters; outputs info
 * Parameters: device num (returned by the List Device fuc), Fw Version MSB, Fw Version LSB
 **/
uint16_t FTDI_OpenDevice(int device_num)
{
	int RTimeout =10;
	int WTimeout =100;
	int VersionMSB =0;
	int VersionLSB =0;
	uint8_t temp[4];
	long version;
	uint8_t major_ver,minor_ver,build_ver;
	int recvd =0;
	unsigned char byte = 0;
	int size = 0;
	int res = 0;
	int tries =0;
	uint8_t latencyTimer;
	FT_STATUS ftStatus;
	int BreakTime;
	int MABTime;
	uint8_t DMX_Data[513];

	// Try at least 3 times
	do  {
		printf("\n------ D2XX ------- Opening [Device %d] ------ Try %d",device_num,tries);
		ftStatus = FT_Open(device_num,&device_handle);
		Sleep(750);
		tries ++;
	} while ((ftStatus != FT_OK) && (tries < 3));

	if (ftStatus == FT_OK)
	{
		// D2XX Driver Version
		ftStatus = FT_GetDriverVersion(device_handle,(LPDWORD)&version);
		if (ftStatus == FT_OK)
		{
			major_ver = (uint8_t) version >> 16;
			minor_ver = (uint8_t) version >> 8;
			build_ver = (uint8_t) version & 0xFF;
			printf("\nD2XX Driver Version:: %02X.%02X.%02X ",major_ver,minor_ver,build_ver);
		}
		else
			printf("Unable to Get D2XX Driver Version") ;

		// Latency Timer
		ftStatus = FT_GetLatencyTimer (device_handle,(PUCHAR)&latencyTimer);
		if (ftStatus == FT_OK)
			printf("\nLatency Timer:: %d ",latencyTimer);
		else
			printf("\nUnable to Get Latency Timer") ;

		FT_SetTimeouts(device_handle,RTimeout,WTimeout);
		FT_Purge (device_handle,FT_PURGE_RX);

		// Send Get Widget Params to get Device Info
		printf("Sending GET_WIDGET_PARAMS packet... ");
		res = FTDI_SendData(GET_WIDGET_PARAMS,(unsigned char *)&size,2);
		if (res == NO_RESPONSE)
		{
			FT_Purge (device_handle,FT_PURGE_TX);
			res = FTDI_SendData(GET_WIDGET_PARAMS,(unsigned char *)&size,2);
			if (res == NO_RESPONSE)
			{
				FTDI_ClosePort();
				return  NO_RESPONSE;
			}
		}
		else
			printf("\n PRO Connected Succesfully");
		// Receive Widget Response
		printf("\nWaiting for GET_WIDGET_PARAMS_REPLY packet... ");
		res=FTDI_ReceiveData(GET_WIDGET_PARAMS_REPLY,(unsigned char *)&PRO_Params,sizeof(DMXUSBPROParamsType));
		if (res == NO_RESPONSE)
		{
			res=FTDI_ReceiveData(GET_WIDGET_PARAMS_REPLY,(unsigned char *)&PRO_Params,sizeof(DMXUSBPROParamsType));
			if (res == NO_RESPONSE)
			{
				FTDI_ClosePort();
				return  NO_RESPONSE;
			}
		}
		else
			printf("\n GET WIDGET REPLY Received ... ");
		// Firmware  Version
		VersionMSB = PRO_Params.FirmwareMSB;
		VersionLSB = PRO_Params.FirmwareLSB;
		// Display All Info avialable
		res = FTDI_SendData(GET_WIDGET_SN,(unsigned char *)&size,2);
		res=FTDI_ReceiveData(GET_WIDGET_SN,(unsigned char *)&temp,4);
		printf("\n-----------::PRO Connected [Information Follows]::------------");
		printf("\n\t\t  FIRMWARE VERSION: %d.%d",VersionMSB,VersionLSB);
		BreakTime = (int) (PRO_Params.BreakTime * 10.67) + 100;
		printf("\n\t\t  BREAK TIME: %d micro sec ",BreakTime);
		MABTime = (int) (PRO_Params.MaBTime * 10.67);
		printf("\n\t\t  MAB TIME: %d micro sec",MABTime);
		printf("\n\t\t  SEND REFRESH RATE: %d packets/sec",PRO_Params.RefreshRate);

		// Send DMX
		memset(DMX_Data,0x1E,sizeof(DMX_Data));
		res = FTDI_SendData(4,(unsigned char *)&DMX_Data,sizeof(DMX_Data));

		return TRUE;
	}
	else // Can't open Device 
		return FALSE;
}